"""Commands. They work in the bot's DM and in the approval group; other groups only get /id."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import texts
from bot.config import Config
from bot.service import PostService
from bot.utils import h

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
DENIED = {
    "staff": "⛔ Ye command sirf authorized admins/approvers ke liye hai.",
    "approver": "⛔ Sirf approvers approve/reject kar sakte hain.",
    "owner": "⛔ Ye command sirf owner ke liye hai.",
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


def register(app: Application, cfg: Config, svc: PostService) -> None:
    def allowed(role: str, uid: int) -> bool:
        return {
            "any": True,
            "staff": cfg.is_staff(uid),
            "approver": cfg.can_approve(uid),
            "owner": cfg.is_owner(uid),
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
        if update.effective_chat.type == ChatType.PRIVATE and not cfg.is_staff(uid):
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
            await reply(update, f"Post #{pid} nahi mila.")
            return
        await reply(update, text)
        if update.effective_chat.type == ChatType.PRIVATE and not await svc.send_preview(
                pid, update.effective_chat.id):
            await reply(update, "ℹ️ Storage channel se preview nahi mil paya.")

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
        await reply(update, "⏸ Publishing PAUSE ho gaya. Queue safe hai. /resume se chalu karo.")

    @command("resume", "owner")
    async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await svc.set_paused(False)
        await reply(update, "▶️ Publishing phir se chalu.")

    @command("setinterval", "owner")
    async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        try:
            mn, mx = float(args[0]), float(args[1])
        except (IndexError, ValueError):
            await reply(update, "Use: /setinterval &lt;min&gt; &lt;max&gt;  (minutes, e.g. /setinterval 10 15)")
            return
        if not (0 < mn <= mx <= 1440):
            await reply(update, "Galat values: 0 &lt; min ≤ max ≤ 1440 hona chahiye.")
            return
        await svc.set_interval(mn, mx)
        await reply(update, f"⏱ Interval ab {mn:g}–{mx:g} min. Agle publish ke baad se lagu hoga.")

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
            await svc.alert_owner("⚠️ Approval group supergroup ban gaya, uski ID badal gayi!\n"
                                  f"Naya APPROVAL_GROUP_ID: <code>{msg.migrate_to_chat_id}</code>\n"
                                  "Secret/.env update karke bot restart karo.")

    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, migrated))
