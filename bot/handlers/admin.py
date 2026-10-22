"""Commands. They work in the bot's DM and in the approval group; other groups only get /id."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import texts
from bot.access import ROLES, Access
from bot.config import Config
from bot.handlers.access import AccessRequests, granted_text
from bot.service import PostService
from bot.utils import h

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
DENIED = {
    "staff": "⛔ This command is only for authorized submitters and approvers.",
    "approver": "⛔ Only approvers can do this.",
    "owner": "⛔ This command is only for the owner.",
}
MAX_LIST = 50


async def reply(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                                  link_preview_options=LinkPreviewOptions(is_disabled=True))


def _post_id(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0].lstrip("#"))
    except ValueError:
        return None


def _target(update: Update, args: list[str], default_role: str) -> tuple[int | None, str | None]:
    """'/adduser 123 approve' or reply to someone's message with '/adduser approve'."""
    uid: int | None = None
    if args and args[0].lstrip("-").isdigit():
        uid, args = int(args[0]), args[1:]
    else:
        reply = update.effective_message.reply_to_message if update.effective_message else None
        if reply and reply.from_user and not reply.from_user.is_bot:
            uid = reply.from_user.id
    role = (args[0].lower() if args else default_role)
    return uid, (role if role in ROLES else None)


def register(app: Application, cfg: Config, svc: PostService, access: Access, requests: AccessRequests) -> None:
    def allowed(role: str, uid: int) -> bool:
        return {
            "any": True,
            "staff": access.is_staff(uid),
            "approver": access.can_approve(uid),
            "owner": access.is_owner(uid),
        }[role]

    def command(name: str, role: str, *, anywhere: bool = False) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
                chat, user = update.effective_chat, update.effective_user
                if chat is None or user is None:
                    return
                if not anywhere and chat.type != ChatType.PRIVATE and chat.id != cfg.approval_group_id:
                    return  # silently ignore other groups
                if not allowed(role, user.id):
                    await reply(update, DENIED[role])
                    return
                try:
                    await fn(update, context)
                except TelegramError as e:
                    log.warning("/%s failed: %s", name, e)
                    await reply(update, f"⚠️ Error: {h(e)}")

            app.add_handler(CommandHandler(name, wrapper, filters=filters.UpdateType.MESSAGE))
            return wrapper

        return deco

    @command("start", "any")
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        if update.effective_chat.type == ChatType.PRIVATE and not access.is_staff(uid):
            await requests.request(update.effective_user)
            await reply(update, texts.unauthorized_text(uid))
            return
        mn, mx = await svc.interval()
        await reply(update, texts.help_text(mn, mx))

    @command("help", "any")
    async def help_(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await start(update, context)

    @command("id", "any", anywhere=True)
    async def id_(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat, user = update.effective_chat, update.effective_user
        title = chat.title or chat.full_name or "-"
        await reply(update, f"💬 Chat: {h(title)}\nChat ID: <code>{chat.id}</code> ({chat.type})\n"
                            f"👤 Aapka user ID: <code>{user.id}</code>")

    @command("stats", "staff")
    async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, await svc.stats_text())

    @command("queue", "staff")
    async def queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, await svc.queue_text())

    @command("list", "staff")
    async def list_(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        which = args[0] if args else "all"
        try:
            n = max(1, min(int(args[1]), MAX_LIST)) if len(args) > 1 else 10
        except ValueError:
            n = 10
        await reply(update, await svc.list_text(which, n))

    @command("post", "staff")
    async def post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None:
            await reply(update, "Use: /post &lt;id&gt;")
            return
        text = await svc.details_text(pid)
        if text is None:
            await reply(update, f"Post #{pid} not found.")
            return
        await reply(update, text)
        if update.effective_chat.type == ChatType.PRIVATE and not await svc.send_preview(
                pid, update.effective_chat.id):
            await reply(update, "ℹ️ Could not load a preview from the storage channel.")

    @command("approve", "approver")
    async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None:
            await reply(update, "Use: /approve &lt;id&gt;")
            return
        _, text = await svc.approve(pid, update.effective_user)
        await reply(update, text)

    @command("reject", "approver")
    async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        pid = _post_id(args)
        if pid is None:
            await reply(update, "Use: /reject &lt;id&gt; [reason]")
            return
        _, text = await svc.reject(pid, update.effective_user, " ".join(args[1:]))
        await reply(update, text)

    @command("postnow", "approver")
    async def postnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None:
            await reply(update, "Use: /postnow &lt;id&gt; – approve (if needed) and publish immediately")
            return
        _, text = await svc.publish_now(pid, update.effective_user)
        await reply(update, text)

    def rest(update: Update) -> str:
        """Text after '/command <id>' with line breaks kept."""
        parts = (update.effective_message.text or "").split(maxsplit=2)
        return parts[2] if len(parts) > 2 else ""

    @command("changes", "approver")
    async def changes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None or not rest(update).strip():
            await reply(update, "Use: /changes &lt;id&gt; &lt;what should be changed&gt;")
            return
        _, text = await svc.request_changes(pid, update.effective_user, rest(update))
        await reply(update, text)

    @command("caption", "approver")
    async def caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        new = rest(update)
        if pid is None or not new.strip():
            await reply(update, "Use: /caption &lt;id&gt; &lt;new caption&gt;  ·  /caption &lt;id&gt; off")
            return
        word = new.strip().lower()
        value = None if word == "off" else "" if word in ("remove", "none", "hide") else new
        _, text = await svc.set_caption(pid, update.effective_user, value)
        await reply(update, text)

    @command("schedule", "approver")
    async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None or not rest(update).strip():
            await reply(update, "Use: /schedule &lt;id&gt; &lt;time&gt;\n"
                                "Examples: 18:30 · 25-10 18:30 · +90 · +2h · off")
            return
        _, text = await svc.schedule_post(pid, update.effective_user, rest(update))
        await reply(update, text)

    @command("top", "approver")
    async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None:
            await reply(update, "Use: /top &lt;id&gt;")
            return
        _, text = await svc.move_to_top(pid, update.effective_user)
        await reply(update, text)

    @command("report", "staff")
    async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, await svc.report_text())

    @command("quiethours", "owner")
    async def quiethours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if args and args[0].lower() == "off":
            await svc.set_quiet_hours(None, None)
            await reply(update, "🌙 Quiet hours turned off.")
            return
        try:
            start, end = int(args[0].split(":")[0]), int(args[1].split(":")[0])
            if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
                raise ValueError
        except (IndexError, ValueError):
            await reply(update, "Use: /quiethours &lt;start hour&gt; &lt;end hour&gt;  (e.g. /quiethours 23 7)  ·  "
                                "/quiethours off")
            return
        await svc.set_quiet_hours(start, end)
        await reply(update, f"🌙 Quiet hours set: {start:02d}:00 – {end:02d}:00 ({cfg.tz_name}). No regular posts in "
                            "this window; posts with a fixed /schedule time and ⚡ Post Now still go out.")

    @command("setfooter", "owner")
    async def setfooter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        parts = (update.effective_message.text or "").split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
        if not text:
            await reply(update, "Use: /setfooter &lt;text&gt;  (e.g. /setfooter Join @mychannel)  ·  /setfooter off")
            return
        if text.lower() == "off":
            await svc.set_footer(None)
            await reply(update, "🔖 Footer turned off.")
            return
        if len(text) > 200:
            await reply(update, "Footer is too long (max 200 characters).")
            return
        await svc.set_footer(text)
        await reply(update, f"🔖 Footer set. It will be added under every caption and text post:\n\n{h(text)}")

    @command("editpost", "staff")
    async def editpost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        new = rest(update)
        if pid is None or not new.strip():
            await reply(update, "Use: /editpost &lt;id&gt; &lt;new caption&gt;  ·  /editpost &lt;id&gt; remove "
                                "(no caption)  ·  /editpost &lt;id&gt; off (original)")
            return
        uid = update.effective_user.id
        word = new.strip().lower()
        value = None if word == "off" else "" if word in ("remove", "none", "hide") else new
        _, text = await svc.edit_post(pid, update.effective_user, value, moderator=access.can_approve(uid))
        await reply(update, text)

    @command("clearbuttons", "owner")
    async def clearbuttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n = max(1, min(int((context.args or ["50"])[0]), 200))
        except ValueError:
            n = 50
        await reply(update, f"🧹 Removing buttons from the last {n} published posts...")
        cleared, failed = await svc.clear_channel_buttons(n)
        await reply(update, f"🧹 Done: buttons removed from {cleared} post(s)"
                            + (f", {failed} could not be changed." if failed else "."))

    @command("replyto", "staff")
    async def replyto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await reply(update, "Use: /replyto &lt;post id or channel post link&gt; – then send your post\n"
                                "e.g. /replyto 12 · /replyto https://t.me/yourchannel/123 · /replyto off\n"
                                "Change a waiting post: /replyto &lt;target&gt; &lt;your post id&gt;")
            return
        user = update.effective_user
        if len(args) >= 2 and args[1].lstrip("#").isdigit():
            _, text = await svc.set_reply(int(args[1].lstrip("#")), user, args[0],
                                          moderator=access.can_approve(user.id))
        elif not access.can_submit(user.id):
            text = "Only people who can submit posts can use /replyto this way."
        else:
            _, text = await svc.arm_reply(user, args[0])
        await reply(update, text)

    @command("preview", "staff")
    async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None:
            await reply(update, "Use: /preview &lt;id&gt; – see how a post will look in the channel")
            return
        _, text = await svc.send_channel_preview(pid, update.effective_chat.id)
        await reply(update, text)

    @command("leaderboard", "staff")
    async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            days = max(1, min(int((context.args or ["30"])[0]), 365))
        except ValueError:
            days = 30
        await reply(update, await svc.leaderboard_text(days))

    @command("format", "owner")
    async def format_(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        arg = (context.args or [""])[0].lower()
        names = {"both": "bold headline + quote", "bold": "bold headline only", "quote": "quote only", "off": "off"}
        if arg not in names:
            await reply(update, "✨ <b>Caption auto-format</b>\n"
                                "/format both – first line bold, the rest in a quote block (default)\n"
                                "/format bold – only the first line bold\n"
                                "/format quote – only the quote block\n"
                                "/format off – post captions exactly as sent\n\n"
                                "Only multi-line captions change; your own bold/links stay. "
                                "Check with /preview &lt;id&gt;.")
            return
        await svc.set_format_style(arg)
        await reply(update, f"✨ Auto-format: <b>{names[arg]}</b>. Applies to posts published from now on.")

    @command("approval", "owner")
    async def approval_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        arg = (context.args or [""])[0].lower()
        if arg not in ("on", "off"):
            state = "ON (every post needs approval)" if await svc.approval_required() else "OFF (auto-approve)"
            await reply(update, f"Approval is currently <b>{state}</b>.\nUse: /approval on · /approval off")
            return
        await svc.set_approval_required(arg == "on")
        await reply(update, "✅ Approval turned ON – new posts wait for an approver." if arg == "on" else
                    "⚡ Approval turned OFF – new posts go straight into the queue (still shown in the approval "
                    "group, where you can Cancel or Post Now).")

    @command("setbutton", "owner")
    async def setbutton(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        parts = (update.effective_message.text or "").split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else ""
        usage = ("Use: /setbutton &lt;text&gt; | &lt;url&gt;  (one button per line, max 5)\n"
                 "e.g. /setbutton 📢 Join Channel | https://t.me/yourchannel\n"
                 "/setbutton join – a Join button for this channel · /setbutton off – no buttons")
        if not raw:
            await reply(update, usage)
            return
        if raw.lower() in ("off", "none"):
            await svc.set_buttons([])
            await reply(update, "🔘 Buttons under new posts turned off. To remove them from posts that are already "
                                "in the channel too: /clearbuttons")
            return
        if raw.lower() in ("join", "default"):
            if await svc.set_buttons(None):
                await reply(update, "🔘 A Join button for this channel will appear under every new post.")
            else:
                await reply(update, "The channel has no public @username, so I can't build a Join link. "
                                    "Use /setbutton &lt;text&gt; | &lt;url&gt; instead.")
            return
        rows = []
        for line in raw.splitlines():
            text, sep, url = line.partition("|")
            text, url = text.strip(), url.strip()
            if not sep or not text or not url.startswith(("https://", "http://", "tg://")) or len(text) > 64:
                await reply(update, f"Could not read: {h(line)}\n\n{usage}")
                return
            rows.append({"text": text, "url": url})
        if len(rows) > 5:
            await reply(update, "Max 5 buttons.")
            return
        await svc.set_buttons(rows)
        await reply(update, f"🔘 {len(rows)} button(s) will appear under every new post (not under albums – "
                            "Telegram does not allow buttons on albums).")

    @command("cancel", "staff")
    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None:
            await reply(update, "Use: /cancel &lt;id&gt;")
            return
        _, text = await svc.cancel(pid, update.effective_user)
        await reply(update, text)

    @command("retry", "owner")
    async def retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pid = _post_id(context.args or [])
        if pid is None:
            await reply(update, "Use: /retry &lt;id&gt;")
            return
        _, text = await svc.retry(pid, update.effective_user)
        await reply(update, text)

    @command("pause", "owner")
    async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await svc.set_paused(True)
        await reply(update, "⏸ Publishing paused. The queue is kept – use /resume to continue.")

    @command("resume", "owner")
    async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await svc.set_paused(False)
        await reply(update, "▶️ Publishing resumed.")

    @command("setinterval", "owner")
    async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        try:
            mn, mx = float(args[0]), float(args[1])
        except (IndexError, ValueError):
            await reply(update, "Use: /setinterval &lt;min&gt; &lt;max&gt;  (minutes, e.g. /setinterval 10 15)")
            return
        if not (0 < mn <= mx <= 1440):
            await reply(update, "Invalid values: need 0 &lt; min ≤ max ≤ 1440.")
            return
        await svc.set_interval(mn, mx)
        await reply(update, f"⏱ Interval set to {mn:g}–{mx:g} min. It applies from the next publish onwards.")

    @command("adduser", "owner")
    async def adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid, role = _target(update, context.args or [], "post")
        if uid is None or role is None:
            await reply(update, "Use: /adduser &lt;user_id&gt; [post|approve|both]\n"
                                "(or reply to someone's message with: /adduser approve)\n"
                                "To find a user ID, ask the user to send /id to the bot.")
            return
        roles = ROLES[role]
        new = await access.grant(uid, roles)
        if not new:
            await reply(update, f"ℹ️ <code>{uid}</code> already has this access.")
            return
        await reply(update, f"✅ Granted access to <code>{uid}</code>: {', '.join(new)}")
        await svc.send(uid, granted_text(new))

    @command("removeuser", "owner")
    async def removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid, role = _target(update, context.args or [], "both")
        if uid is None or role is None:
            await reply(update, "Use: /removeuser &lt;user_id&gt; [post|approve|both]")
            return
        removed, from_env = await access.revoke(uid, ROLES[role])
        lines = []
        if removed:
            lines.append(f"🗑 Removed access from <code>{uid}</code>: {', '.join(removed)}")
        if from_env:
            lines.append(f"⚠️ {', '.join(from_env)} access comes from the env / GitHub secret "
                         "(SUBMITTER_IDS / APPROVER_IDS / "
                         "OWNER_ID) – remove it there, then restart the bot.")
        await reply(update, "\n".join(lines) or f"ℹ️ <code>{uid}</code> had no access granted through the bot.")

    @command("users", "owner")
    async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        s = access.summary()

        def ids(xs: list[int]) -> str:
            return ", ".join(f"<code>{x}</code>" for x in xs) or "-"

        await reply(update, "👥 <b>Access list</b>\n"
                            f"👑 Owner: <code>{cfg.owner_id}</code>\n\n"
                            f"📤 Can post (added via bot): {ids(s['added_submitters'])}\n"
                            f"👮 Can approve (added via bot): {ids(s['added_approvers'])}\n\n"
                            f"📤 Can post (env/secret): {ids(s['env_submitters'])}\n"
                            f"👮 Can approve (env/secret): {ids(s['env_approvers'])}\n\n"
                            "To remove someone: /removeuser &lt;user_id&gt;")

    # /id posted inside a channel: DM the owner instead of answering in the channel, then tidy up
    async def channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, chat = update.channel_post, update.effective_chat
        if msg is None or chat is None:
            return
        await svc.alert_owner(f"📣 Channel: {h(chat.title)}\nChat ID: <code>{chat.id}</code> ({chat.type})")
        try:
            await msg.delete()
        except TelegramError:
            pass

    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.Regex(r"^/id(@\w+)?\s*$"),
                                   channel_id))

    # basic group upgraded to supergroup -> the approval group's ID changed
    async def migrated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg and msg.migrate_to_chat_id and msg.chat_id == cfg.approval_group_id:
            await svc.alert_owner("⚠️ The approval group was upgraded to a supergroup, so its ID changed!\n"
                                  f"New APPROVAL_GROUP_ID: <code>{msg.migrate_to_chat_id}</code>\n"
                                  "Update the secret / .env and restart the bot.")

    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, migrated))
