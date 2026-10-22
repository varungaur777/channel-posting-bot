"""Buttons under the DM preview: the submitter fixes the caption before the post goes out.

✏️ Edit caption  -> the bot asks for the new caption (ForceReply); the reply is applied
🧹 Remove caption · ↩️ Original caption · 🚫 Cancel post -> applied at once
After a change, a fresh preview is sent. Works for the post's submitter and for approvers.
"""

from __future__ import annotations

import logging

from telegram import ForceReply, Message, Update
from telegram.constants import ChatType
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot import texts
from bot.access import Access
from bot.config import Config
from bot.service import PostService
from bot.utils import h

log = logging.getLogger(__name__)


class DmEditor:
    def __init__(self, svc: PostService, access: Access) -> None:
        self.svc = svc
        self.access = access
        self.open: dict[tuple[int, int], int] = {}  # (chat id, prompt message id) -> post id
        self.ai_options: dict[tuple[int, int], list[str]] = {}  # (chat id, post id) -> last AI suggestions

    async def ask_caption(self, chat_id: int, post_id: int) -> None:
        prompt = await self.svc.send(
            chat_id, f"✏️ Reply to this message with the new caption for <b>Post #{post_id}</b>.",
            reply_markup=ForceReply(input_field_placeholder="New caption"))
        if prompt is not None:
            self.open[(chat_id, prompt.message_id)] = post_id

    async def apply(self, chat_id: int, user: object, post_id: int, value: str | None) -> bool:
        ok, text = await self.svc.edit_post(post_id, user, value, moderator=self.access.can_approve(user.id))
        if ok:
            await self.svc.send_dm_preview(post_id, chat_id, f"{text}\n🔄 Updated preview:")
        else:
            await self.svc.send(chat_id, text)
        return ok

    async def try_handle_reply(self, msg: Message, user: object) -> bool:
        """True if ``msg`` answered one of our caption prompts (then it is NOT a new submission)."""
        if msg.reply_to_message is None:
            return False
        post_id = self.open.pop((msg.chat_id, msg.reply_to_message.message_id), None)
        if post_id is None:
            return False
        if not msg.text:
            await self.svc.send(msg.chat_id, "Please send the new caption as text.")
            self.open[(msg.chat_id, msg.reply_to_message.message_id)] = post_id
            return True
        await self.apply(msg.chat_id, user, post_id, msg.text)
        return True


def register(app: Application, cfg: Config, svc: PostService, access: Access) -> DmEditor:
    editor = DmEditor(svc, access)

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.message is None or q.message.chat.type != ChatType.PRIVATE:
            if q is not None:
                await q.answer()
            return
        parts = (q.data or "").split(":")
        action, raw = parts[0], parts[-1]
        post_id, chat_id, user = int(raw), q.message.chat.id, q.from_user
        if action == "dmai":
            await q.answer("🤖 Writing suggestions... (a few seconds)")
            options, error = await svc.ai_suggest(post_id, user, parts[1], moderator=access.can_approve(user.id))
            if not options:
                await svc.send(chat_id, error)
                return
            editor.ai_options[(chat_id, post_id)] = options
            body = "\n\n".join(f"<b>{i + 1}.</b>\n{h(opt)}" for i, opt in enumerate(options))
            await svc.send(chat_id, f"🤖 <b>AI suggestions for Post #{post_id}</b>\n\n{body}\n\nPick one to use:",
                           reply_markup=texts.ai_options_keyboard(post_id, len(options)))
            return
        if action == "dmu":
            choice = parts[1]
            options = editor.ai_options.get((chat_id, post_id))
            if choice == "x":
                editor.ai_options.pop((chat_id, post_id), None)
                await q.answer("Kept your caption.")
                return
            if not options or not choice.isdigit() or int(choice) >= len(options):
                await q.answer("These suggestions expired – ask the AI again.", show_alert=True)
                return
            await q.answer("Using this caption...")
            editor.ai_options.pop((chat_id, post_id), None)
            await editor.apply(chat_id, user, post_id, options[int(choice)])
            return
        if action == "dme":
            await q.answer()
            await editor.ask_caption(chat_id, post_id)
        elif action == "dmr":
            await q.answer("Removing the caption...")
            await editor.apply(chat_id, user, post_id, "")
        elif action == "dmo":
            await q.answer("Restoring the original caption...")
            await editor.apply(chat_id, user, post_id, None)
        elif action == "dmx":
            ok, text = await svc.cancel(post_id, user)
            await q.answer(text[:190], show_alert=not ok)
            if ok:
                await svc.send(chat_id, text)
        else:
            await q.answer()

    app.add_handler(CallbackQueryHandler(
        on_button, pattern=r"^(dm[eorx]:\d+|dmai:(improve|translate|hashtags):\d+|dmu:(\d|x):\d+)$"))
    return editor
