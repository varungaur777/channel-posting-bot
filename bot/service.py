"""Post lifecycle: submit -> approve/reject -> publish, notifications, and startup recovery.

Handlers and the scheduler only call this class. Every status change is an atomic
``Database.transition`` (filtered on the expected current status).

    pending --approve--> approved ("scheduled") --claim--> publishing --> published
       |                    |   ^                             |
       |                    |   +---- retry backoff / 429 ----+
       +--reject--> rejected|                                 +--> failed        (permanent / too many retries)
       +--cancel--> cancelled <--cancel--+                    +--> needs_review  (timeout / stop mid-send)
    failed / needs_review --/retry (owner)--> approved
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    ReplyParameters,
)
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from bot import texts
from bot.config import Config
from bot.db import (
    APPROVED,
    CANCELLED,
    CHANGES_REQUESTED,
    FAILED,
    NEEDS_REVIEW,
    PENDING,
    PUBLISHED,
    PUBLISHING,
    REJECTED,
    STATUSES,
    Database,
)
from bot.schedule_utils import in_quiet, parse_when, quiet_window, skip_quiet
from bot.storage import ContentMissing, CopyIncomplete, copy_batch, copy_exact, is_missing_error, rebuild, rollback
from bot.utils import (
    CAPTIONABLE,
    STYLES,
    apply_caption,
    content_hash,
    content_type,
    extract_items,
    fmt_delta,
    fmt_dt,
    h,
    truncate,
    utcnow,
)

log = logging.getLogger(__name__)

MAX_BACKOFF_MIN = 30
TG_LIMIT = 4096
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

Eta = tuple[datetime, datetime]


def _seconds(value: Any) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


def _name(user: Any) -> str:
    return getattr(user, "full_name", None) or getattr(user, "first_name", None) or str(user.id)


class PostService:
    def __init__(self, cfg: Config, db: Database, bot: Any, *, clock: Callable[[], datetime] = utcnow,
                 rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.bot = bot
        self.clock = clock
        self.rng = rng or random.Random()
        self.tz = cfg.tz
        self._submit_lock = asyncio.Lock()
        self.default_buttons: list[dict] = []
        self.reply_targets: dict[int, dict] = {}  # user id -> {"post": id} or {"message_id": id} (/replyto)

    # ================================================================== messaging helpers
    async def send(self, chat_id: int, text: str, **kwargs: Any) -> Message | None:
        try:
            return await self.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                                               link_preview_options=_NO_PREVIEW, **kwargs)
        except Forbidden as e:
            log.warning("cannot message %s (bot blocked / never started?): %s", chat_id, e)
        except TelegramError as e:
            log.warning("send to %s failed: %s", chat_id, e)
        return None

    async def alert_owner(self, text: str, *, quiet: bool = False) -> None:
        await self.send(self.cfg.owner_id, text, disable_notification=quiet)

    async def notify_submitter(self, post: dict, text: str) -> None:
        if post.get("submitter_id"):
            await self.send(post["submitter_id"], text)

    async def refresh_control(self, post: dict | None, eta: Eta | None = None) -> None:
        """Re-render the control message in the approval group (buttons only while pending)."""
        if not post or not post.get("control_message_id"):
            return
        state = await self.db.get_state()
        if post["status"] == APPROVED and eta is None:
            eta = await self.eta_for(post["_id"])
        text = texts.render_control(post, self.tz, eta=eta, paused=bool(state.get("paused")))
        markup = (texts.approval_keyboard(post["_id"]) if post["status"] == PENDING
                  else texts.scheduled_keyboard(post["_id"]) if post["status"] == APPROVED else None)
        try:
            await self.bot.edit_message_text(chat_id=self.cfg.approval_group_id,
                                             message_id=post["control_message_id"], text=text,
                                             parse_mode=ParseMode.HTML, reply_markup=markup,
                                             link_preview_options=_NO_PREVIEW)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("could not update control message of #%s: %s", post["_id"], e)
        except TelegramError as e:
            log.warning("could not update control message of #%s: %s", post["_id"], e)

    # ================================================================== schedule / ETA
    async def interval(self) -> tuple[float, float]:
        st = await self.db.get_state()
        return (float(st.get("min_interval") or self.cfg.min_interval_min),
                float(st.get("max_interval") or self.cfg.max_interval_min))

    async def schedule(self) -> list[tuple[dict, Eta]]:
        """Scheduled posts in publish order with an (earliest, latest) ETA window."""
        now = self.clock()
        queue = await self.db.queue()
        state = await self.db.get_state()
        mn, mx = await self.interval()
        quiet = quiet_window(state)
        npa = state.get("next_publish_at")
        early = late = max(now, npa) if npa else now
        out: list[tuple[dict, Eta]] = []
        k = 0
        for post in queue:
            if post.get("scheduled_for"):  # fixed time: not part of the gap sequence
                out.append((post, (post["scheduled_for"], post["scheduled_for"])))
                continue
            if k:
                early += timedelta(minutes=mn)
                late += timedelta(minutes=mx)
            k += 1
            early, late = skip_quiet(early, quiet, self.tz), skip_quiet(late, quiet, self.tz)
            nat = post.get("next_attempt_at")
            e, l_ = (max(early, nat), max(late, nat)) if nat else (early, late)
            out.append((post, (e, l_)))
        out.sort(key=lambda pe: (pe[1][0], pe[0]["_id"]))
        return out

    async def eta_for(self, post_id: int) -> Eta | None:
        for post, eta in await self.schedule():
            if post["_id"] == post_id:
                return eta
        return None

    # ================================================================== submission
    async def submit(self, user: Any, messages: Sequence[Message]) -> int | None:
        """A submitter's DM (one message or one whole album) -> pending post in the approval group."""
        messages = sorted(messages, key=lambda m: m.message_id)
        chat_id = messages[0].chat_id
        source_ids = [m.message_id for m in messages]
        reply = ReplyParameters(message_id=source_ids[0], allow_sending_without_reply=True)
        items = extract_items(messages)
        chash = content_hash(items)
        now = self.clock()

        reply_target = self.reply_targets.pop(user.id, None)  # set by /replyto
        async with self._submit_lock:  # duplicate check + insert must not interleave
            dup = await self.db.find_duplicate(chash, now - timedelta(days=self.cfg.dup_window_days))
            if dup:
                await self.send(chat_id, f"⚠️ This content was already submitted as <b>Post #{dup['_id']}</b> "
                                         f"({texts.status_text(dup['status'])}). "
                                         "Duplicates are not allowed.", reply_parameters=reply)
                return None
            pid = await self.db.next_post_id()
            post = {
                "_id": pid, "status": PENDING,
                "submitter_id": user.id, "submitter_name": _name(user),
                "submitter_username": getattr(user, "username", None),
                "source_chat_id": chat_id, "source_message_ids": source_ids,
                "media_group_id": messages[0].media_group_id,
                "content_type": content_type(items), "content_hash": chash, "items": items,
                "storage_message_ids": [], "approval_message_ids": [], "control_message_id": None,
                "created_at": now, "updated_at": now,
                "decided_at": None, "decided_by": None, "decided_by_name": None, "reject_reason": None,
                "cancelled_by": None, "cancelled_at": None,
                "publishing_started_at": None, "published_at": None, "channel_message_ids": [],
                "retries": 0, "next_attempt_at": None, "last_error": None,
                "scheduled_for": None, "caption_override": None, "reminded_at": None,
                "reply_to_post": (reply_target or {}).get("post"),
                "reply_to_message_id": (reply_target or {}).get("message_id"),
                "history": [{"at": now, "event": "submitted", "by": user.id}],
            }
            await self.db.insert_post(post)
        log.info("post #%s submitted by %s (%s)", pid, user.id, post["content_type"])

        # 1) permanent snapshot in the storage channel
        try:
            storage_ids = await copy_exact(self.bot, self.cfg.storage_channel_id, chat_id, source_ids)
        except (TelegramError, CopyIncomplete) as e:
            await self._fail_submission(post, f"storage channel copy failed: {e}", reply)
            return None
        post = await self.db.update_post(pid, {"storage_message_ids": storage_ids})

        # 2) copy from storage into the approval group + control message with buttons
        approval_ids: list[int] = []
        try:
            approval_ids = await copy_exact(self.bot, self.cfg.approval_group_id, self.cfg.storage_channel_id,
                                            storage_ids)
            post = await self.db.update_post(pid, {"approval_message_ids": approval_ids})
            control = await self.bot.send_message(
                chat_id=self.cfg.approval_group_id, text=texts.render_control(post, self.tz),
                parse_mode=ParseMode.HTML, reply_markup=texts.approval_keyboard(pid),
                reply_parameters=ReplyParameters(message_id=approval_ids[0], allow_sending_without_reply=True),
                link_preview_options=_NO_PREVIEW,
            )
        except (TelegramError, CopyIncomplete) as e:
            await rollback(self.bot, self.cfg.approval_group_id, approval_ids)
            await self._fail_submission(post, f"approval group copy failed: {e}", reply)
            return None
        post = await self.db.update_post(pid, {"control_message_id": control.message_id})
        if not await self.approval_required():
            auto = await self.db.transition(
                pid, [PENDING], APPROVED,
                {"decided_at": self.clock(), "decided_by": None, "decided_by_name": None, "auto_approved": True,
                 "retries": 0, "next_attempt_at": None},
                event="auto_approved",
            )
            if auto is not None:
                eta = await self.eta_for(pid)
                await self.refresh_control(auto, eta)
                await self.send_dm_preview(pid, chat_id, f"✅ <b>Post #{pid}</b> submitted and added to the queue "
                                                         "(approval is currently off).\n"
                                                         f"⏰ Expected in the channel: {texts.fmt_eta(eta, self.tz)}")
                return pid
        reply_note = ""
        if post.get("reply_to_post"):
            reply_note = f"\n↩️ It will be published as a reply to Post #{post['reply_to_post']}."
        elif post.get("reply_to_message_id"):
            reply_note = f"\n↩️ It will be published as a reply to channel message {post['reply_to_message_id']}."
        await self.send_dm_preview(pid, chat_id, f"✅ <b>Post #{pid}</b> submitted!\nIt has been sent for approval. "
                                                 f"I'll let you know here once it is approved or rejected.{reply_note}")
        return pid

    async def _fail_submission(self, post: dict, error: str, reply: ReplyParameters) -> None:
        log.error("post #%s submission failed: %s", post["_id"], error)
        await self.db.transition(post["_id"], [PENDING], FAILED, {"last_error": error}, event="submit_failed")
        await self.send(post["source_chat_id"], f"❌ Your post could not be saved: {h(truncate(error, 300))}\n"
                                                "Please try again in a little while.", reply_parameters=reply)
        await self.alert_owner(f"⚠️ Post #{post['_id']} submission FAILED\n{h(truncate(error, 500))}")

    async def handle_edit(self, message: Message) -> None:
        post = await self.db.find_by_source(message.chat_id, message.message_id)
        if post is None:
            return
        await self.send(message.chat_id,
                        f"✏️ Edits are not tracked – this change will not affect <b>Post #{post['_id']}</b>.\n"
                        f"To change it, use /cancel {post['_id']} and send a new post.",
                        reply_parameters=ReplyParameters(message_id=message.message_id,
                                                         allow_sending_without_reply=True))

    # ================================================================== decisions
    async def _why_not(self, post_id: int) -> str:
        post = await self.db.get_post(post_id)
        if post is None:
            return f"Post #{post_id} not found."
        return f"Post #{post_id} is already {texts.status_text(post['status'])}."

    async def approve(self, post_id: int, user: Any) -> tuple[bool, str]:
        post = await self.db.transition(
            post_id, [PENDING], APPROVED,
            {"decided_at": self.clock(), "decided_by": user.id, "decided_by_name": _name(user),
             "retries": 0, "next_attempt_at": None},
            event="approved", by=user.id,
        )
        if post is None:
            return False, await self._why_not(post_id)
        log.info("post #%s approved by %s", post_id, user.id)
        state = await self.db.get_state()
        eta = await self.eta_for(post_id)
        eta_txt = texts.fmt_eta(eta, self.tz, bool(state.get("paused")))
        await self.refresh_control(post, eta)
        await self.notify_submitter(post, f"✅ Your <b>Post #{post_id}</b> has been approved!\n"
                                          f"⏰ Expected in the channel: {eta_txt}")
        return True, f"✅ Post #{post_id} approved. ETA: {eta_txt}"

    async def reject(self, post_id: int, user: Any, reason: str | None = None) -> tuple[bool, str]:
        reason = (reason or "").strip() or None
        post = await self.db.transition(
            post_id, [PENDING], REJECTED,
            {"decided_at": self.clock(), "decided_by": user.id, "decided_by_name": _name(user),
             "reject_reason": reason},
            event="rejected", by=user.id,
        )
        if post is None:
            return False, await self._why_not(post_id)
        log.info("post #%s rejected by %s", post_id, user.id)
        await self.refresh_control(post)
        await self.notify_submitter(post, f"❌ Your <b>Post #{post_id}</b> was rejected."
                                          + (f"\n💬 Reason: {h(reason)}" if reason else ""))
        return True, f"❌ Post #{post_id} rejected."

    async def cancel(self, post_id: int, user: Any) -> tuple[bool, str]:
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        if not (self.cfg.is_owner(user.id) or post.get("submitter_id") == user.id):
            return False, "Only the owner or the person who submitted the post can cancel it."
        post = await self.db.transition(post_id, [PENDING, APPROVED], CANCELLED,
                                        {"cancelled_by": user.id, "cancelled_at": self.clock()},
                                        event="cancelled", by=user.id)
        if post is None:
            return False, (await self._why_not(post_id)) + " It can no longer be cancelled."
        log.info("post #%s cancelled by %s", post_id, user.id)
        await self.refresh_control(post)
        if post.get("submitter_id") != user.id:
            await self.notify_submitter(post, f"🚫 Your <b>Post #{post_id}</b> was cancelled by the owner.")
        return True, f"🚫 Post #{post_id} cancelled. It will never be published."

    async def retry(self, post_id: int, user: Any) -> tuple[bool, str]:
        if not self.cfg.is_owner(user.id):
            return False, "Only the owner can use /retry."
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        if not post.get("decided_by"):
            return False, (f"Post #{post_id} was never approved (its submission failed). "
                           "Ask the submitter to send it again.")
        post = await self.db.transition(post_id, [FAILED, NEEDS_REVIEW], APPROVED,
                                        {"retries": 0, "next_attempt_at": None}, event="retry", by=user.id)
        if post is None:
            return False, (await self._why_not(post_id)) + " /retry only works on failed or needs_review posts."
        log.info("post #%s re-queued by owner", post_id)
        eta = await self.eta_for(post_id)
        await self.refresh_control(post, eta)
        return True, f"🔁 Post #{post_id} is back in the queue. ETA: {texts.fmt_eta(eta, self.tz)}"

    async def publish_now(self, post_id: int, user: Any) -> tuple[bool, str]:
        """Approve (if still pending) and publish immediately, ignoring the gap, backoff and pause.

        Callers check that ``user`` is an approver. Afterwards the next scheduled post waits the normal gap.
        """
        now = self.clock()
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        if post["status"] == PENDING:
            approved = await self.db.transition(
                post_id, [PENDING], APPROVED,
                {"decided_at": now, "decided_by": user.id, "decided_by_name": _name(user),
                 "retries": 0, "next_attempt_at": None},
                event="approved", by=user.id,
            )
            if approved is None:
                return False, await self._why_not(post_id)
        claimed = await self.db.transition(post_id, [APPROVED], PUBLISHING,
                                           {"publishing_started_at": now, "posted_now": True},
                                           event="publish_now", by=user.id)
        if claimed is None:
            return False, (await self._why_not(post_id)) + " Only pending or scheduled posts can be posted now."
        log.info("post #%s: publish now requested by %s", post_id, user.id)
        if await self._publish_claimed(claimed) == post_id:
            return True, f"⚡ Post #{post_id} published now. The next scheduled post waits the normal gap after it."
        after = await self.db.get_post(post_id) or {}
        reason = f" ({truncate(after['last_error'], 150)})" if after.get("last_error") else ""
        return False, f"⚠️ Post #{post_id} could not be published right now – status: " \
                      f"{texts.status_text(after.get('status', '?'))}{reason}"

    # ================================================================== editing decisions
    async def request_changes(self, post_id: int, user: Any, note: str) -> tuple[bool, str]:
        """Instead of rejecting: tell the submitter what to change. The post is closed; they resend."""
        note = (note or "").strip()
        if not note:
            return False, "Please write what should be changed."
        post = await self.db.transition(
            post_id, [PENDING], CHANGES_REQUESTED,
            {"decided_at": self.clock(), "decided_by": user.id, "decided_by_name": _name(user),
             "changes_note": note},
            event="changes_requested", by=user.id,
        )
        if post is None:
            return False, await self._why_not(post_id)
        log.info("post #%s: changes requested by %s", post_id, user.id)
        await self.refresh_control(post)
        await self.notify_submitter(post, f"✏️ <b>Changes requested</b> for your <b>Post #{post_id}</b>:\n"
                                          f"{h(note)}\n\nPlease make these changes and send the updated post "
                                          "to me again.")
        return True, f"✏️ Changes requested for Post #{post_id}. The submitter has been notified."

    async def set_caption(self, post_id: int, user: Any, text: str | None, *, notify: bool = True
                          ) -> tuple[bool, str]:
        """Replace the caption (or the text of a text post) used when publishing.

        ``None`` restores the original, ``""`` removes the caption (media posts only)."""
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        if post["status"] not in (PENDING, APPROVED):
            return False, await self._why_not(post_id)
        items = post.get("items") or []
        if text is not None:
            text = text.strip()
            is_text = len(items) == 1 and items[0]["media_type"] == "text"
            limit = 4096 if is_text else 1024
            if not text and is_text:
                return False, "A text post cannot be empty – use /cancel instead."
            if len(text) > limit:
                return False, f"Too long: {len(text)} characters (limit {limit})."
            if apply_caption(items, text, None) is None:
                return False, "This type of post cannot have a caption."
        post = await self.db.transition(
            post_id, [post["status"]], post["status"],
            {"caption_override": text, "caption_edited_by_name": _name(user) if text is not None else None},
            event="caption_restored" if text is None else "caption_removed" if text == "" else "caption_edited",
            by=user.id,
        )
        if post is None:
            return False, await self._why_not(post_id)
        await self.refresh_control(post)
        if text is None:
            return True, f"📝 Post #{post_id} will use its original caption again."
        if notify and post.get("submitter_id") != user.id:
            what = "removed the caption of" if text == "" else "edited the caption of"
            await self.notify_submitter(post, f"📝 An approver {what} your <b>Post #{post_id}</b>"
                                              + (f":\n{h(truncate(text, 500))}" if text else "."))
        if text == "":
            return True, f"📝 Post #{post_id} will be published without a caption."
        return True, f"📝 Caption of Post #{post_id} updated. It will be published with the new caption."

    async def schedule_post(self, post_id: int, user: Any, when_text: str) -> tuple[bool, str]:
        """Publish at a fixed time ('18:30', '25-10 18:30', '+90', '+2h') or 'off' to go back to the queue."""
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        off = when_text.strip().lower() in ("off", "clear", "none", "cancel")
        when = None if off else parse_when(when_text, self.clock(), self.tz)
        if not off and when is None:
            return False, ("Could not read that time. Examples: 18:30 · 25-10 18:30 · +90 (minutes) · +2h · off")
        fields: dict[str, Any] = {"scheduled_for": when}
        if post["status"] == PENDING:  # scheduling a pending post approves it
            fields.update(decided_at=self.clock(), decided_by=user.id, decided_by_name=_name(user),
                          retries=0, next_attempt_at=None)
        elif post["status"] != APPROVED:
            return False, await self._why_not(post_id)
        updated = await self.db.transition(post_id, [post["status"]], APPROVED, fields,
                                           event="scheduled_at" if when else "schedule_cleared", by=user.id)
        if updated is None:
            return False, await self._why_not(post_id)
        await self.refresh_control(updated)
        if when is None:
            return True, f"🗓 Post #{post_id} is back in the normal queue."
        msg = f"🕒 Your <b>Post #{post_id}</b> will be published at {fmt_dt(when, self.tz)}."
        await self.notify_submitter(updated, ("✅ Approved! " if post["status"] == PENDING else "") + msg)
        return True, f"🕒 Post #{post_id} will be published at {fmt_dt(when, self.tz)}."

    async def move_to_top(self, post_id: int, user: Any) -> tuple[bool, str]:
        queue = [p for p in await self.db.queue() if not p.get("scheduled_for")]
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        if post["status"] != APPROVED:
            return False, await self._why_not(post_id) + " Only scheduled posts can be moved."
        first = min((p["decided_at"] for p in queue if p.get("decided_at")), default=self.clock())
        updated = await self.db.transition(
            post_id, [APPROVED], APPROVED,
            {"decided_at": min(first, post.get("decided_at") or first) - timedelta(seconds=1),
             "scheduled_for": None, "next_attempt_at": None},
            event="moved_to_top", by=user.id,
        )
        if updated is None:
            return False, await self._why_not(post_id)
        eta = await self.eta_for(post_id)
        await self.refresh_control(updated, eta)
        return True, f"⬆️ Post #{post_id} is now first in the queue. ETA: {texts.fmt_eta(eta, self.tz)}"

    async def edit_post(self, post_id: int, user: Any, text: str | None, *, moderator: bool) -> tuple[bool, str]:
        """Change the caption/text of a post - before publishing (used at publish time) or after (edited in the
        channel). Allowed for moderators (owner/approvers) and for the post's own submitter, no approval needed.
        ``text=None`` restores the original caption."""
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        if not moderator and post.get("submitter_id") != user.id:
            return False, "You can only edit your own posts."
        if post["status"] in (PENDING, APPROVED):
            return await self.set_caption(post_id, user, text, notify=moderator)
        if post["status"] != PUBLISHED:
            return False, (await self._why_not(post_id)) + " Only pending, scheduled or published posts can be edited."
        items = post.get("items") or []
        is_text = len(items) == 1 and items[0]["media_type"] == "text"
        if text is not None:
            text = text.strip()
            limit = 4096 if is_text else 1024
            if not text and is_text:
                return False, "A text post cannot be empty."
            if len(text) > limit:
                return False, f"The new caption must be at most {limit} characters."
        state = await self.db.get_state()
        footer, style = state.get("footer"), format_style(state)
        if text is None:  # back to the original caption (+ formatting + footer)
            edited = apply_caption(items, None, footer, style) or [dict(i) for i in items]
        else:
            edited = apply_caption(items, text, footer, style)
        if edited is None:
            return False, "This type of post cannot have a caption."
        ids = post.get("channel_message_ids") or []
        # the message that carries the caption: the one that had it originally, else the first captionable one
        idx = 0 if is_text else next(
            (i for i, it in enumerate(items) if it.get("caption")),
            next((i for i, it in enumerate(items) if it["media_type"] in CAPTIONABLE), 0))
        if idx >= len(ids):
            return False, "Could not find this post's message in the channel."
        markup = await self.post_buttons() if len(ids) == 1 else None
        try:
            if is_text:
                await self.bot.edit_message_text(chat_id=self.cfg.channel_id, message_id=ids[0],
                                                 text=edited[0]["text"],
                                                 entities=_ents_or_none(edited[0].get("entities"), self.bot),
                                                 reply_markup=markup, link_preview_options=None)
            else:
                await self.bot.edit_message_caption(chat_id=self.cfg.channel_id, message_id=ids[idx],
                                                    caption=edited[idx].get("caption") or "",
                                                    caption_entities=_ents_or_none(
                                                        edited[idx].get("caption_entities"), self.bot),
                                                    reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                return False, f"⚠️ Telegram did not accept the edit: {h(e)}"
        except TelegramError as e:
            return False, f"⚠️ Could not edit the channel post: {h(e)}"
        updated = await self.db.transition(
            post_id, [PUBLISHED], PUBLISHED,
            {"caption_override": text, "caption_edited_by_name": _name(user) if text is not None else None},
            event="edited_in_channel", by=user.id,
        )
        await self.refresh_control(updated)
        if moderator and post.get("submitter_id") != user.id:
            await self.notify_submitter(post, f"📝 The caption of your published <b>Post #{post_id}</b> was edited.")
        log.info("post #%s edited in the channel by %s", post_id, user.id)
        if text == "":
            return True, f"✏️ Caption of Post #{post_id} removed in the channel."
        return True, f"✏️ Post #{post_id} updated in the channel."

    async def parse_reply_target(self, arg: str) -> tuple[dict | None, str]:
        """'12' / '#12' (a post of this bot) or a channel link 'https://t.me/<channel>/123'."""
        arg = arg.strip()
        link = _CHANNEL_LINK.search(arg)
        if link:
            return {"message_id": int(link.group(1))}, f"channel message {link.group(1)}"
        try:
            target_id = int(arg.lstrip("#"))
        except ValueError:
            return None, "Give a post number (e.g. 12) or a channel post link (e.g. https://t.me/yourchannel/123)."
        target = await self.db.get_post(target_id)
        if target is None:
            return None, f"Post #{target_id} not found."
        if target["status"] not in (PENDING, APPROVED, PUBLISHING, PUBLISHED):
            return None, (await self._why_not(target_id)) + " It will never be in the channel."
        return {"post": target_id}, f"Post #{target_id}"

    async def arm_reply(self, user: Any, arg: str) -> tuple[bool, str]:
        """/replyto <target>: the user's NEXT submission becomes a reply to that channel post."""
        if arg.strip().lower() in ("off", "cancel", "none"):
            self.reply_targets.pop(user.id, None)
            return True, "↩️ Reply mode cancelled – your next post will be a normal post."
        target, label = await self.parse_reply_target(arg)
        if target is None:
            return False, label
        self.reply_targets[user.id] = target
        return True, (f"↩️ OK – send your post now. It will appear in the channel as a <b>reply to {label}</b>.\n"
                      "(/replyto off to cancel)")

    async def set_reply(self, post_id: int, user: Any, arg: str, *, moderator: bool) -> tuple[bool, str]:
        """/replyto <target> <post id>: make an existing pending/scheduled post a reply (or 'off')."""
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        if not moderator and post.get("submitter_id") != user.id:
            return False, "You can only change your own posts."
        if post["status"] not in (PENDING, APPROVED):
            return False, (await self._why_not(post_id)) + " Only pending or scheduled posts can be changed."
        if arg.strip().lower() in ("off", "none"):
            target, label = {}, ""
        else:
            target, label = await self.parse_reply_target(arg)
            if target is None:
                return False, label
            if target.get("post") == post_id:
                return False, "A post cannot reply to itself."
        updated = await self.db.transition(
            post_id, [post["status"]], post["status"],
            {"reply_to_post": target.get("post"), "reply_to_message_id": target.get("message_id")},
            event="reply_set", by=user.id,
        )
        if updated is None:
            return False, await self._why_not(post_id)
        await self.refresh_control(updated)
        return True, (f"↩️ Post #{post_id} will be a reply to {label}." if label
                      else f"↩️ Post #{post_id} will be a normal post (not a reply).")

    async def send_channel_preview(self, post_id: int, chat_id: int) -> tuple[bool, str]:
        """Send the post to ``chat_id`` exactly as it would look in the channel (caption edits, formatting,
        footer, buttons). Nothing is changed and nothing is posted to the channel."""
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} not found."
        state = await self.db.get_state()
        items = post.get("items") or []
        edited = apply_caption(items, post.get("caption_override"), state.get("footer"), format_style(state))
        markup = await self.post_buttons()
        try:
            if edited is None and post.get("storage_message_ids"):
                await copy_batch(self.bot, chat_id, self.cfg.storage_channel_id, post["storage_message_ids"], markup)
            else:
                await rebuild(self.bot, chat_id, edited or items, markup)
        except (TelegramError, ContentMissing, CopyIncomplete) as e:
            return False, f"⚠️ Could not build the preview: {h(e)}"
        return True, f"👀 This is how <b>Post #{post_id}</b> will look in the channel."

    async def send_dm_preview(self, post_id: int, chat_id: int, text: str) -> None:
        """After submitting / editing in DM: the post as it will look in the channel, then ``text`` with
        buttons to edit the caption right there (before it is approved or published)."""
        ok, _ = await self.send_channel_preview(post_id, chat_id)
        note = "\n\n👆 Above is how it will look in the channel. Want to change the caption first?" if ok else ""
        await self.send(chat_id, text + note, reply_markup=texts.dm_edit_keyboard(post_id))

    async def leaderboard_text(self, days: int = 30) -> str:
        since = self.clock() - timedelta(days=days)
        rows: dict[int, dict] = {}
        for post in await self.db.posts_where({"created_at": {"$gte": since}}):
            uid = post.get("submitter_id")
            r = rows.setdefault(uid, {"name": post.get("submitter_name") or str(uid), "sent": 0, "published": 0,
                                      "waiting": 0, "rejected": 0, "changes": 0})
            r["sent"] += 1
            st = post["status"]
            if st == PUBLISHED:
                r["published"] += 1
            elif st in (PENDING, APPROVED, PUBLISHING):
                r["waiting"] += 1
            elif st == REJECTED:
                r["rejected"] += 1
            elif st == CHANGES_REQUESTED:
                r["changes"] += 1
        if not rows:
            return f"🏆 No posts in the last {days} days."
        ranked = sorted(rows.values(), key=lambda r: (-r["published"], -r["sent"], r["name"]))
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 <b>Leaderboard – last {days} days</b>", ""]
        for i, r in enumerate(ranked):
            rate = f"{round(100 * r['published'] / r['sent'])}%" if r["sent"] else "-"
            lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} <b>{h(r['name'])}</b>")
            lines.append(f"    📤 {r['sent']} sent · 📢 {r['published']} published ({rate}) · ⏳ {r['waiting']} waiting"
                         f" · ❌ {r['rejected']} rejected · ✏️ {r['changes']} changes")
        return _fit(lines)

    # ================================================================== settings
    async def set_paused(self, paused: bool) -> None:
        await self.db.set_state(paused=paused)
        log.info("publishing %s", "paused" if paused else "resumed")

    async def approval_required(self) -> bool:
        return (await self.db.get_state()).get("approval_required", True) is not False

    async def set_approval_required(self, required: bool) -> None:
        await self.db.set_state(approval_required=required)
        log.info("approval %s", "required" if required else "turned off (auto-approve)")

    async def load_channel_info(self) -> None:
        """Remember the channel's public link for /setbutton join."""
        try:
            chat = await self.bot.get_chat(self.cfg.channel_id)
        except TelegramError as e:
            log.warning("could not read channel info: %s", e)
            return
        if chat.username:
            self.default_buttons = [{"text": f"📢 Join {chat.title}", "url": f"https://t.me/{chat.username}"}]

    async def post_buttons(self) -> InlineKeyboardMarkup | None:
        """Buttons under every channel post: off unless set with /setbutton (or /setbutton join)."""
        state = await self.db.get_state()
        rows = state.get("buttons") or []
        if not rows:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], url=b["url"])] for b in rows])

    async def set_buttons(self, rows: list[dict] | None) -> bool:
        """``None`` = the automatic Join button (False if the channel has no public link), ``[]`` = no buttons."""
        if rows is None:
            if not self.default_buttons:
                await self.load_channel_info()
            if not self.default_buttons:
                return False
            rows = self.default_buttons
        await self.db.set_state(buttons=rows)
        return True

    async def clear_channel_buttons(self, limit: int = 50) -> tuple[int, int]:
        """Remove inline buttons from the last ``limit`` published single-message posts. Returns (cleared, failed)."""
        posts = await self.db.list_posts([PUBLISHED], limit)
        cleared = failed = 0
        for post in posts:
            ids = post.get("channel_message_ids") or []
            if len(ids) != 1:
                continue  # albums never had buttons
            try:
                await self.bot.edit_message_reply_markup(chat_id=self.cfg.channel_id, message_id=ids[0],
                                                         reply_markup=None)
                cleared += 1
            except RetryAfter as e:
                await asyncio.sleep(_seconds(e.retry_after) + 1)
                failed += 1
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    failed += 1
            except TelegramError:
                failed += 1
            await asyncio.sleep(0.3)
        log.info("cleared buttons from %d channel post(s), %d failed", cleared, failed)
        return cleared, failed

    async def set_format_style(self, style: str) -> None:
        await self.db.set_state(format_style=style)

    async def set_footer(self, text: str | None) -> None:
        await self.db.set_state(footer=(text or "").strip() or None)

    async def set_quiet_hours(self, start: int | None, end: int | None) -> None:
        await self.db.set_state(quiet_start=start, quiet_end=end)

    async def set_interval(self, mn: float, mx: float) -> None:
        await self.db.set_state(min_interval=mn, max_interval=mx)
        log.info("interval set to %s-%s min", mn, mx)

    # ================================================================== publishing
    async def tick(self) -> int | None:
        """One scheduler step: publish at most one due post. Returns its id when published."""
        state = await self.db.get_state()
        if state.get("paused"):
            return None
        now = self.clock()
        candidate = await self.db.next_timed(now)  # a fixed publish time wins over the gap and quiet hours
        if candidate is None:
            npa = state.get("next_publish_at")
            if npa and now < npa:
                return None
            if in_quiet(now, quiet_window(state), self.tz):
                return None
            candidate = await self.db.next_due(now)
        if candidate is None:
            return None
        post = await self.db.transition(candidate["_id"], [APPROVED], PUBLISHING,
                                        {"publishing_started_at": now, "posted_now": False}, event="publishing")
        if post is None:  # cancelled / claimed in the meantime
            return None
        return await self._publish_claimed(post)

    async def _publish_claimed(self, post: dict) -> int | None:
        pid = post["_id"]
        try:
            channel_ids = await self._deliver(post)
        except asyncio.CancelledError:
            await self._needs_review(post, "the bot stopped while this post was being published – "
                                           "it may already be in the channel")
            raise
        except RetryAfter as e:
            await self._flood_wait(post, _seconds(e.retry_after))
        except TimedOut as e:  # may have been delivered: never auto-retry
            await self._needs_review(post, f"Telegram timed out (the post may already be in the channel): {e}")
        except CopyIncomplete as e:
            await self._backoff(post, str(e))
        except ContentMissing as e:
            await self._fail(post, f"content missing: {e}")
        except (BadRequest, Forbidden, ChatMigrated, InvalidToken) as e:
            await self._fail(post, f"{type(e).__name__}: {e}")
        except NetworkError as e:
            await self._backoff(post, f"network error: {e}")
        except TelegramError as e:
            await self._fail(post, f"{type(e).__name__}: {e}")
        except Exception as e:  # unknown state: a human has to look
            log.exception("unexpected error publishing #%s", pid)
            await self._needs_review(post, f"unexpected error: {e!r}")
        else:
            await self._published(post, channel_ids)
            return pid
        return None

    async def _deliver(self, post: dict) -> list[int]:
        """Copy from the storage snapshot; rebuild from saved file_ids when the snapshot is gone."""
        storage_ids = post.get("storage_message_ids") or []
        channel = self.cfg.channel_id
        state = await self.db.get_state()
        footer, style = state.get("footer"), format_style(state)
        markup = await self.post_buttons()
        reply_to = await self._reply_message_id(post)
        items = post.get("items") or []
        edited = apply_caption(items, post.get("caption_override"), footer, style)
        if edited is not None or (reply_to and len(storage_ids) > 1):
            # new caption / footer, or an album that must be a reply (copyMessages cannot reply):
            # re-send from the saved file_ids
            return await rebuild(self.bot, channel, edited or items, markup, reply_to)
        if storage_ids:
            try:
                sent = await copy_batch(self.bot, channel, self.cfg.storage_channel_id, storage_ids, markup,
                                        reply_to)
            except BadRequest as e:
                if not is_missing_error(e):
                    raise
                log.warning("storage snapshot of #%s is missing (%s) - rebuilding from file_ids", post["_id"], e)
            else:
                if len(sent) == len(storage_ids):
                    return sent
                await rollback(self.bot, channel, sent)
                log.warning("only %d/%d storage messages of #%s copied - rebuilding from file_ids",
                            len(sent), len(storage_ids), post["_id"])
        return await rebuild(self.bot, channel, items, markup, reply_to)

    async def _reply_message_id(self, post: dict) -> int | None:
        """Channel message this post should reply to (a channel link, or another post once it is published)."""
        if post.get("reply_to_message_id"):
            return int(post["reply_to_message_id"])
        target_id = post.get("reply_to_post")
        if not target_id:
            return None
        target = await self.db.get_post(int(target_id))
        ids = (target or {}).get("channel_message_ids") or []
        if target and target["status"] == PUBLISHED and ids:
            return ids[0]
        log.warning("post #%s should reply to #%s, which is not published - posting without reply",
                    post["_id"], target_id)
        return None

    async def _published(self, post: dict, channel_ids: list[int]) -> None:
        now = self.clock()
        mn, mx = await self.interval()
        next_at = now + timedelta(minutes=self.rng.uniform(mn, mx))
        current = (await self.db.get_state()).get("next_publish_at")
        if current and current > next_at:  # a Post Now never pulls the regular queue earlier
            next_at = current
        updated = await self.db.transition(post["_id"], [PUBLISHING], PUBLISHED,
                                           {"published_at": now, "channel_message_ids": channel_ids,
                                            "next_attempt_at": None})
        await self.db.set_state(last_published_at=now, next_publish_at=next_at)
        log.info("post #%s published (channel msgs %s); next slot %s", post["_id"], channel_ids, next_at.isoformat())
        await self.refresh_control(updated)
        await self.notify_submitter(post, f"📢 Your <b>Post #{post['_id']}</b> has been published to the channel!\n"
                                          f"✏️ To change its caption: /editpost {post['_id']} &lt;new caption&gt;")

    async def _flood_wait(self, post: dict, seconds: float) -> None:
        until = self.clock() + timedelta(seconds=seconds + 1)
        log.warning("flood wait %.0fs while publishing #%s", seconds, post["_id"])
        await self.db.transition(post["_id"], [PUBLISHING], APPROVED,
                                 {"next_attempt_at": until, "last_error": f"Telegram flood wait {seconds:.0f}s"},
                                 event="flood_wait")
        state = await self.db.get_state()
        npa = state.get("next_publish_at")
        if not npa or npa < until:  # the whole bot is rate-limited, hold every post
            await self.db.set_state(next_publish_at=until)

    async def _backoff(self, post: dict, error: str) -> None:
        retries = int(post.get("retries") or 0) + 1
        if retries > self.cfg.max_retries:
            await self._fail(post, f"{error} (still failing after {self.cfg.max_retries} retries)",
                             fields={"retries": retries})
            return
        delay = timedelta(minutes=min(2 ** (retries - 1), MAX_BACKOFF_MIN))
        log.warning("post #%s attempt failed (%s) - retry %d in %s", post["_id"], error, retries, delay)
        updated = await self.db.transition(post["_id"], [PUBLISHING], APPROVED,
                                           {"retries": retries, "next_attempt_at": self.clock() + delay,
                                            "last_error": error}, event="retry_scheduled")
        await self.refresh_control(updated)

    async def _fail(self, post: dict, error: str, fields: dict | None = None) -> None:
        log.error("post #%s FAILED: %s", post["_id"], error)
        updated = await self.db.transition(post["_id"], [PUBLISHING], FAILED, {"last_error": error, **(fields or {})},
                                           event="failed")
        await self.refresh_control(updated)
        await self.alert_owner(f"⚠️ <b>Post #{post['_id']} FAILED</b>\n{h(truncate(error, 500))}\n\n"
                               f"Fix the cause, then use /retry {post['_id']}.")
        await self.notify_submitter(post, f"⚠️ Your <b>Post #{post['_id']}</b> could not be published. "
                                          "The owner has been notified.")

    async def _needs_review(self, post: dict, error: str) -> None:
        log.error("post #%s NEEDS REVIEW: %s", post["_id"], error)
        updated = await self.db.transition(post["_id"], [PUBLISHING], NEEDS_REVIEW, {"last_error": error},
                                           event="needs_review")
        await self.refresh_control(updated)
        await self.alert_owner(
            f"🔍 <b>Post #{post['_id']} needs review</b>\n{h(truncate(error, 500))}\n\n"
            "Check the channel: if the post is there, do nothing; if it is missing, use "
            f"/retry {post['_id']}")

    # ================================================================== startup
    async def recover_on_startup(self) -> None:
        """Fix states left by a crash / kill. Must run only while holding the instance lease."""
        stuck = []
        for post in await self.db.posts_with_status(PUBLISHING):
            upd = await self.db.transition(
                post["_id"], [PUBLISHING], NEEDS_REVIEW,
                {"last_error": "the bot restarted while this post was being published – "
                               "it may already be in the channel"},
                event="recovered")
            if upd:
                stuck.append(post["_id"])
                await self.refresh_control(upd)
        if stuck:
            ids = ", ".join(f"#{i}" for i in stuck)
            await self.alert_owner(f"🔍 These posts were being published when the bot restarted: {ids}\n"
                                   "They are now marked <b>needs_review</b>. "
                                   "Check the channel and use /retry &lt;id&gt; "
                                   "for any post that is missing.")

        orphans = []
        for post in await self.db.posts_with_status(PENDING):
            if post.get("control_message_id"):
                continue
            upd = await self.db.transition(post["_id"], [PENDING], FAILED,
                                           {"last_error": "submission was interrupted by a bot restart"},
                                           event="recovered")
            if upd:
                orphans.append(post["_id"])
                await self.notify_submitter(upd, f"⚠️ Your <b>Post #{post['_id']}</b> did not reach approval "
                                                 "because the bot restarted. "
                                                 "Please send it again.")
        if orphans:
            await self.alert_owner("⚠️ Interrupted submissions marked as failed: "
                                   + ", ".join(f"#{i}" for i in orphans))

        for post in await self.db.posts_with_status(APPROVED):
            if not post.get("decided_at"):
                await self.db.update_post(post["_id"], {"decided_at": post.get("created_at") or self.clock()})
        if stuck or orphans:
            log.warning("startup recovery: needs_review=%s failed=%s", stuck, orphans)

    # ================================================================== read-only views
    async def stats_text(self) -> str:
        counts = await self.db.count_by_status()
        state = await self.db.get_state()
        mn, mx = await self.interval()
        npa = state.get("next_publish_at")
        now = self.clock()
        slot = "now (as soon as a post is ready)" if not npa or npa <= now else \
            f"{fmt_dt(npa, self.tz)} (in {fmt_delta(npa - now)})"
        lines = ["📊 <b>Stats</b>"]
        lines += [f"{texts.status_text(s)}: <b>{counts.get(s, 0)}</b>" for s in STATUSES]
        lines += [
            "",
            f"⏱ Interval: {mn:g}–{mx:g} min",
            f"⏭ Next publish slot: {slot}",
            f"📢 Last published: {fmt_dt(state.get('last_published_at'), self.tz)}",
            f"⏸ Paused: {'YES' if state.get('paused') else 'no'}",
            f"🌙 Quiet hours: {_quiet_text(quiet_window(state))}",
            f"🔖 Footer: {h(truncate(state['footer'], 60)) if state.get('footer') else 'off'}",
            f"✨ Auto-format: {_STYLE_TEXT[format_style(state)]}",
            f"✅ Approval: {'required' if state.get('approval_required', True) is not False else 'OFF (auto-approve)'}",
        ]
        return "\n".join(lines)

    async def queue_text(self) -> str:
        sched = await self.schedule()
        state = await self.db.get_state()
        if not sched:
            return "🗓 The queue is empty – no scheduled posts."
        head = f"🗓 <b>Queue ({len(sched)})</b>" + (" – ⏸ PAUSED" if state.get("paused") else "")
        lines = [head]
        for i, (post, eta) in enumerate(sched, 1):
            when = (f"🕒 at {fmt_dt(post['scheduled_for'], self.tz)}" if post.get("scheduled_for")
                    else f"ETA {texts.fmt_eta(eta, self.tz)}")
            lines.append(f"{i}. <b>#{post['_id']}</b> · {h(post.get('content_type'))} · {when}"
                         + (f" · 🔁{post['retries']}" if post.get("retries") else ""))
        return _fit(lines)

    async def list_text(self, which: str, limit: int) -> str:
        which = which.lower()
        status = texts.LIST_ALIASES.get(which, which)
        if status != "all" and status not in STATUSES:
            return "Unknown status. Use: pending|scheduled|published|rejected|cancelled|failed|needs_review|changes|all"
        posts = await self.db.list_posts(None if status == "all" else [status], limit)
        if not posts:
            return f"No posts found ({h(which)})."
        lines = [f"📋 <b>{h(which)}</b> – latest {len(posts)}"]
        lines += [texts.render_list_line(p, self.tz) for p in posts]
        return _fit(lines)

    async def details_text(self, post_id: int) -> str | None:
        post = await self.db.get_post(post_id)
        if post is None:
            return None
        state = await self.db.get_state()
        eta = await self.eta_for(post_id) if post["status"] == APPROVED else None
        return texts.render_details(post, self.tz, eta, bool(state.get("paused")))

    async def send_preview(self, post_id: int, chat_id: int) -> bool:
        post = await self.db.get_post(post_id)
        if not post or not post.get("storage_message_ids"):
            return False
        try:
            sent = await copy_batch(self.bot, chat_id, self.cfg.storage_channel_id, post["storage_message_ids"])
        except TelegramError as e:
            log.warning("preview of #%s failed: %s", post_id, e)
            return False
        return bool(sent)

    async def housekeeping(self) -> None:
        """Called by the scheduler every tick: pending reminders and the daily report."""
        now = self.clock()
        if self.cfg.pending_reminder_min > 0:
            cutoff = now - timedelta(minutes=self.cfg.pending_reminder_min)
            for post in await self.db.posts_where({"status": PENDING, "reminded_at": None,
                                                   "created_at": {"$lte": cutoff}}):
                if not post.get("control_message_id"):
                    continue
                await self.db.update_post(post["_id"], {"reminded_at": now})
                waited = fmt_delta(now - post["created_at"])
                await self.send(self.cfg.approval_group_id,
                                f"⏰ <b>Post #{post['_id']}</b> has been waiting for approval for {waited}.",
                                reply_parameters=ReplyParameters(message_id=post["control_message_id"],
                                                                 allow_sending_without_reply=True))
        hour = self.cfg.daily_report_hour
        if 0 <= hour <= 23:
            local = now.astimezone(self.tz)
            today = local.strftime("%Y-%m-%d")
            state = await self.db.get_state()
            if local.hour >= hour and state.get("last_report_date") != today:
                await self.db.set_state(last_report_date=today)
                await self.alert_owner(await self.report_text(), quiet=True)

    async def report_text(self, hours: int = 24) -> str:
        now = self.clock()
        since = now - timedelta(hours=hours)
        db = self.db
        submitted = await db.count({"created_at": {"$gte": since}})
        published = await db.count({"status": PUBLISHED, "published_at": {"$gte": since}})
        rejected = await db.count({"status": REJECTED, "decided_at": {"$gte": since}})
        changes = await db.count({"status": CHANGES_REQUESTED, "decided_at": {"$gte": since}})
        failed = await db.count({"status": {"$in": [FAILED, NEEDS_REVIEW]}, "updated_at": {"$gte": since}})
        pending = await db.count({"status": PENDING})
        queued = await db.count({"status": APPROVED})
        state = await db.get_state()
        npa = state.get("next_publish_at")
        return "\n".join([
            f"📈 <b>Report – last {hours} hours</b> ({fmt_dt(now, self.tz)})",
            f"📥 Submitted: <b>{submitted}</b>",
            f"📢 Published: <b>{published}</b>",
            f"❌ Rejected: <b>{rejected}</b> · ✏️ Changes requested: <b>{changes}</b>",
            f"⚠️ Failed / needs review: <b>{failed}</b>",
            "",
            f"⏳ Waiting for approval now: <b>{pending}</b>",
            f"🗓 In the queue now: <b>{queued}</b>",
            f"⏭ Next publish slot: {fmt_dt(npa, self.tz) if npa and npa > now else 'now'}",
            f"⏸ Paused: {'YES' if state.get('paused') else 'no'}",
        ])

    async def notify_restart(self) -> None:
        await self.alert_owner(f"🔄 Bot restarted (profile: {self.cfg.runtime_profile}).", quiet=True)


_STYLE_TEXT = {"both": "bold headline + quote", "bold": "bold headline", "quote": "quote", "off": "off"}


def format_style(state: dict) -> str:
    """Caption auto-format style; on ('both') unless the owner changed it with /format."""
    style = state.get("format_style") or "both"
    return style if style in STYLES else "both"


_CHANNEL_LINK = re.compile(r"t\.me/(?:c/\d+|[A-Za-z0-9_]+)/(\d+)")


def _ents_or_none(raw: list[dict] | None, bot: Any) -> list[MessageEntity] | None:
    return [MessageEntity.de_json(e, bot) for e in raw] if raw else None


def _quiet_text(window: tuple[int, int] | None) -> str:
    return f"{window[0]:02d}:00 – {window[1]:02d}:00 (no regular posts)" if window else "off"


def _fit(lines: list[str], limit: int = TG_LIMIT - 96) -> str:
    """Join lines but stay under Telegram's 4096-char message limit."""
    out: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > limit:
            out.append(f"… and {len(lines) - i} more (message limit). Use /list or /post.")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)
