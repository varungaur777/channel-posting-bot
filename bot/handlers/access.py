"""Access requests: an unknown user DMs the bot -> the owner gets buttons to grant access in one tap."""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot.access import APPROVE, POST, ROLES, Access
from bot.config import Config
from bot.service import PostService
from bot.utils import h

log = logging.getLogger(__name__)

ROLE_TEXT = {POST: "post bhejna", APPROVE: "approve/reject karna"}


def request_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Post access", callback_data=f"acc:post:{uid}"),
         InlineKeyboardButton("👮 Approver", callback_data=f"acc:approve:{uid}")],
        [InlineKeyboardButton("🤝 Dono", callback_data=f"acc:both:{uid}"),
         InlineKeyboardButton("❌ Ignore", callback_data=f"acc:no:{uid}")],
    ])


def granted_text(roles: list[str] | tuple[str, ...]) -> str:
    lines = ["🎉 Aapko access mil gaya! Ab aap " + " aur ".join(ROLE_TEXT[r] for r in roles) + " kar sakte ho."]
    if POST in roles:
        lines.append("📤 Bas mujhe yahan DM mein post bhejo. /help se sab commands dekho.")
    if APPROVE in roles:
        lines.append("👮 Approve karne ke liye approval group ka member hona zaroori hai – owner se add karwa lo.")
    return "\n".join(lines)


class AccessRequests:
    """Sends at most one request per user per run to the owner."""

    def __init__(self, cfg: Config, svc: PostService, access: Access) -> None:
        self.cfg = cfg
        self.svc = svc
        self.access = access
        self.sent: set[int] = set()

    async def request(self, user: Any) -> bool:
        if user.id in self.sent or self.access.is_staff(user.id):
            return False
        self.sent.add(user.id)
        uname = f" @{h(user.username)}" if getattr(user, "username", None) else ""
        await self.svc.send(
            self.cfg.owner_id,
            f"🙋 <b>Access request</b>\n👤 {h(user.full_name)}{uname}\n🆔 <code>{user.id}</code>\n\n"
            "Is user ko kya access dena hai?",
            reply_markup=request_keyboard(user.id),
        )
        log.info("access request from %s sent to owner", user.id)
        return True


def register(app: Application, cfg: Config, svc: PostService, access: Access) -> None:
    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if q.message is None or q.message.chat.type != ChatType.PRIVATE or not access.is_owner(q.from_user.id):
            await q.answer("Sirf owner access de sakta hai.", show_alert=True)
            return
        _, choice, raw = (q.data or "").split(":")
        uid = int(raw)
        base = q.message.text_html if getattr(q.message, "text_html", None) else h(getattr(q.message, "text", ""))
        if choice == "no":
            result = "❌ Ignore kiya."
        else:
            roles = ROLES[choice]
            new = await access.grant(uid, roles)
            result = ("✅ Access diya: " + ", ".join(roles)) if new else "ℹ️ Is user ke paas ye access pehle se hai."
            if new:
                await svc.send(uid, granted_text(roles))
        try:
            await q.edit_message_text(f"{base}\n\n<b>{result}</b>", parse_mode=ParseMode.HTML)
        except TelegramError as e:
            log.warning("could not update access request message: %s", e)
        await q.answer(result[:190])

    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^acc:(post|approve|both|no):\d+$"))
