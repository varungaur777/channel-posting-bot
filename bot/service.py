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
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from telegram import LinkPreviewOptions, Message, ReplyParameters
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
    FAILED,
    NEEDS_REVIEW,
    PENDING,
    PUBLISHED,
    PUBLISHING,
    REJECTED,
    STATUSES,
    Database,
)
from bot.storage import ContentMissing, CopyIncomplete, copy_batch, copy_exact, is_missing_error, rebuild, rollback
from bot.utils import content_hash, content_type, extract_items, fmt_delta, fmt_dt, h, truncate, utcnow

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
        markup = texts.approval_keyboard(post["_id"]) if post["status"] == PENDING else None
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
        npa = state.get("next_publish_at")
        early = late = max(now, npa) if npa else now
        out: list[tuple[dict, Eta]] = []
        for k, post in enumerate(queue):
            if k:
                early += timedelta(minutes=mn)
                late += timedelta(minutes=mx)
            nat = post.get("next_attempt_at")
            e, l_ = (max(early, nat), max(late, nat)) if nat else (early, late)
            out.append((post, (e, l_)))
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

        async with self._submit_lock:  # duplicate check + insert must not interleave
            dup = await self.db.find_duplicate(chash, now - timedelta(days=self.cfg.dup_window_days))
            if dup:
                await self.send(chat_id, f"⚠️ Ye content pehle hi <b>Post #{dup['_id']}</b> "
                                         f"({texts.status_text(dup['status'])}) mein bheja ja chuka hai. "
                                         "Duplicate allow nahi hai.", reply_parameters=reply)
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
        await self.db.update_post(pid, {"control_message_id": control.message_id})
        await self.send(chat_id, f"✅ <b>Post #{pid}</b> submit ho gaya!\nApproval group mein bhej diya hai. "
                                 "Approve/reject hone pe aapko yahin bataunga.", reply_parameters=reply)
        return pid

    async def _fail_submission(self, post: dict, error: str, reply: ReplyParameters) -> None:
        log.error("post #%s submission failed: %s", post["_id"], error)
        await self.db.transition(post["_id"], [PENDING], FAILED, {"last_error": error}, event="submit_failed")
        await self.send(post["source_chat_id"], f"❌ Post save nahi ho paya: {h(truncate(error, 300))}\n"
                                                "Thodi der baad dobara bhejo.", reply_parameters=reply)
        await self.alert_owner(f"⚠️ Post #{post['_id']} submission FAILED\n{h(truncate(error, 500))}")

    async def handle_edit(self, message: Message) -> None:
        post = await self.db.find_by_source(message.chat_id, message.message_id)
        if post is None:
            return
        await self.send(message.chat_id,
                        f"✏️ Edit track nahi hota – <b>Post #{post['_id']}</b> pe iska koi asar nahi padega.\n"
                        f"Change chahiye to /cancel {post['_id']} karke naya post bhejo.",
                        reply_parameters=ReplyParameters(message_id=message.message_id,
                                                         allow_sending_without_reply=True))

    # ================================================================== decisions
    async def _why_not(self, post_id: int) -> str:
        post = await self.db.get_post(post_id)
        if post is None:
            return f"Post #{post_id} nahi mila."
        return f"Post #{post_id} pehle se {texts.status_text(post['status'])} hai."

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
        await self.notify_submitter(post, f"✅ Aapka <b>Post #{post_id}</b> approve ho gaya!\n"
                                          f"⏰ Channel mein ETA: {eta_txt}")
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
        await self.notify_submitter(post, f"❌ Aapka <b>Post #{post_id}</b> reject ho gaya."
                                          + (f"\n💬 Reason: {h(reason)}" if reason else ""))
        return True, f"❌ Post #{post_id} rejected."

    async def cancel(self, post_id: int, user: Any) -> tuple[bool, str]:
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} nahi mila."
        if not (self.cfg.is_owner(user.id) or post.get("submitter_id") == user.id):
            return False, "Sirf owner ya post bhejne wala hi cancel kar sakta hai."
        post = await self.db.transition(post_id, [PENDING, APPROVED], CANCELLED,
                                        {"cancelled_by": user.id, "cancelled_at": self.clock()},
                                        event="cancelled", by=user.id)
        if post is None:
            return False, (await self._why_not(post_id)) + " Ab cancel nahi ho sakta."
        log.info("post #%s cancelled by %s", post_id, user.id)
        await self.refresh_control(post)
        if post.get("submitter_id") != user.id:
            await self.notify_submitter(post, f"🚫 Aapka <b>Post #{post_id}</b> owner ne cancel kar diya.")
        return True, f"🚫 Post #{post_id} cancel ho gaya. Ye kabhi publish nahi hoga."

    async def retry(self, post_id: int, user: Any) -> tuple[bool, str]:
        if not self.cfg.is_owner(user.id):
            return False, "Sirf owner /retry kar sakta hai."
        post = await self.db.get_post(post_id)
        if post is None:
            return False, f"Post #{post_id} nahi mila."
        if not post.get("decided_by"):
            return False, (f"Post #{post_id} kabhi approve hi nahi hua tha (submission fail hua). "
                           "Submitter se dobara bhejne ko bolo.")
        post = await self.db.transition(post_id, [FAILED, NEEDS_REVIEW], APPROVED,
                                        {"retries": 0, "next_attempt_at": None}, event="retry", by=user.id)
        if post is None:
            return False, (await self._why_not(post_id)) + " /retry sirf failed/needs_review pe chalta hai."
        log.info("post #%s re-queued by owner", post_id)
        eta = await self.eta_for(post_id)
        await self.refresh_control(post, eta)
        return True, f"🔁 Post #{post_id} wapas queue mein. ETA: {texts.fmt_eta(eta, self.tz)}"

    # ================================================================== settings
    async def set_paused(self, paused: bool) -> None:
        await self.db.set_state(paused=paused)
        log.info("publishing %s", "paused" if paused else "resumed")

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
        npa = state.get("next_publish_at")
        if npa and now < npa:
            return None
        candidate = await self.db.next_due(now)
        if candidate is None:
            return None
        post = await self.db.transition(candidate["_id"], [APPROVED], PUBLISHING,
                                        {"publishing_started_at": now}, event="publishing")
        if post is None:  # cancelled / claimed in the meantime
            return None
        return await self._publish_claimed(post)

    async def _publish_claimed(self, post: dict) -> int | None:
        pid = post["_id"]
        try:
            channel_ids = await self._deliver(post)
        except asyncio.CancelledError:
            await self._needs_review(post, "bot band hua jab ye publish ho raha tha – shayad channel mein aa gaya ho")
            raise
        except RetryAfter as e:
            await self._flood_wait(post, _seconds(e.retry_after))
        except TimedOut as e:  # may have been delivered: never auto-retry
            await self._needs_review(post, f"Telegram timeout (post shayad channel mein aa gaya ho): {e}")
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
        if storage_ids:
            try:
                sent = await copy_batch(self.bot, channel, self.cfg.storage_channel_id, storage_ids)
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
        return await rebuild(self.bot, channel, post.get("items") or [])

    async def _published(self, post: dict, channel_ids: list[int]) -> None:
        now = self.clock()
        mn, mx = await self.interval()
        next_at = now + timedelta(minutes=self.rng.uniform(mn, mx))
        updated = await self.db.transition(post["_id"], [PUBLISHING], PUBLISHED,
                                           {"published_at": now, "channel_message_ids": channel_ids,
                                            "next_attempt_at": None})
        await self.db.set_state(last_published_at=now, next_publish_at=next_at)
        log.info("post #%s published (channel msgs %s); next slot %s", post["_id"], channel_ids, next_at.isoformat())
        await self.refresh_control(updated)
        await self.notify_submitter(post, f"📢 Aapka <b>Post #{post['_id']}</b> channel mein publish ho gaya!")

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
            await self._fail(post, f"{error} ({self.cfg.max_retries} retries ke baad bhi fail)",
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
                               f"Theek karke /retry {post['_id']} kar sakte ho.")
        await self.notify_submitter(post, f"⚠️ Aapka <b>Post #{post['_id']}</b> publish nahi ho paya. "
                                          "Owner ko bata diya gaya hai.")

    async def _needs_review(self, post: dict, error: str) -> None:
        log.error("post #%s NEEDS REVIEW: %s", post["_id"], error)
        updated = await self.db.transition(post["_id"], [PUBLISHING], NEEDS_REVIEW, {"last_error": error},
                                           event="needs_review")
        await self.refresh_control(updated)
        await self.alert_owner(
            f"🔍 <b>Post #{post['_id']} needs review</b>\n{h(truncate(error, 500))}\n\n"
            "Channel check karo: post aa gaya hai to kuch mat karo; nahi aaya to "
            f"/retry {post['_id']}")

    # ================================================================== startup
    async def recover_on_startup(self) -> None:
        """Fix states left by a crash / kill. Must run only while holding the instance lease."""
        stuck = []
        for post in await self.db.posts_with_status(PUBLISHING):
            upd = await self.db.transition(
                post["_id"], [PUBLISHING], NEEDS_REVIEW,
                {"last_error": "bot restart hua jab ye publish ho raha tha – shayad channel mein aa gaya ho"},
                event="recovered")
            if upd:
                stuck.append(post["_id"])
                await self.refresh_control(upd)
        if stuck:
            ids = ", ".join(f"#{i}" for i in stuck)
            await self.alert_owner(f"🔍 Restart ke time ye post publish ho rahe the: {ids}\n"
                                   "Inka status <b>needs_review</b> kar diya. Channel check karke /retry &lt;id&gt; "
                                   "karo agar post nahi aaya.")

        orphans = []
        for post in await self.db.posts_with_status(PENDING):
            if post.get("control_message_id"):
                continue
            upd = await self.db.transition(post["_id"], [PENDING], FAILED,
                                           {"last_error": "submission adhoori reh gayi (bot restart)"},
                                           event="recovered")
            if upd:
                orphans.append(post["_id"])
                await self.notify_submitter(upd, f"⚠️ Aapka <b>Post #{post['_id']}</b> bot restart ki wajah se "
                                                 "approval tak nahi pahuncha. Please dobara bhejo.")
        if orphans:
            await self.alert_owner("⚠️ Adhoore submissions failed mark kiye: "
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
        slot = "abhi (jaise hi koi post ready ho)" if not npa or npa <= now else \
            f"{fmt_dt(npa, self.tz)} (in {fmt_delta(npa - now)})"
        lines = ["📊 <b>Stats</b>"]
        lines += [f"{texts.status_text(s)}: <b>{counts.get(s, 0)}</b>" for s in STATUSES]
        lines += [
            "",
            f"⏱ Interval: {mn:g}–{mx:g} min",
            f"⏭ Next publish slot: {slot}",
            f"📢 Last published: {fmt_dt(state.get('last_published_at'), self.tz)}",
            f"⏸ Paused: {'HAAN' if state.get('paused') else 'nahi'}",
        ]
        return "\n".join(lines)

    async def queue_text(self) -> str:
        sched = await self.schedule()
        state = await self.db.get_state()
        if not sched:
            return "🗓 Queue khaali hai – koi scheduled post nahi."
        head = f"🗓 <b>Queue ({len(sched)})</b>" + (" – ⏸ PAUSED" if state.get("paused") else "")
        lines = [head]
        for i, (post, eta) in enumerate(sched, 1):
            lines.append(f"{i}. <b>#{post['_id']}</b> · {h(post.get('content_type'))} · ETA "
                         f"{texts.fmt_eta(eta, self.tz)}"
                         + (f" · 🔁{post['retries']}" if post.get("retries") else ""))
        return _fit(lines)

    async def list_text(self, which: str, limit: int) -> str:
        which = which.lower()
        status = texts.LIST_ALIASES.get(which, which)
        if status != "all" and status not in STATUSES:
            return "Status galat hai. Use: pending|scheduled|published|rejected|cancelled|failed|needs_review|all"
        posts = await self.db.list_posts(None if status == "all" else [status], limit)
        if not posts:
            return f"Koi post nahi mila ({h(which)})."
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

    async def notify_restart(self) -> None:
        await self.alert_owner(f"🔄 Bot restart hua (profile: {self.cfg.runtime_profile}).", quiet=True)


def _fit(lines: list[str], limit: int = TG_LIMIT - 96) -> str:
    """Join lines but stay under Telegram's 4096-char message limit."""
    out: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > limit:
            out.append(f"… aur {len(lines) - i} (limit). /list ya /post use karo.")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)
