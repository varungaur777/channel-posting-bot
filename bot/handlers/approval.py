"""Buttons on control messages in the approval group.

Pending post:   ✅ Approve · ❌ Reject · ✏️ Request Changes · 📝 Edit Caption · ⚡ Approve & Post Now
Scheduled post: ⚡ Post Now · 🕒 Schedule · ⬆️ Move to Top · 📝 Edit Caption · 🚫 Cancel

Buttons that need text (changes, caption, schedule) post a ForceReply prompt; the approver's reply to that
prompt carries the text. Open prompts live in memory - after a restart use the matching command instead.
"""

from __future__ import annotations

import logging

from telegram import ForceReply, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from bot.access import Access
from bot.config import Config
from bot.service import PostService

log = logging.getLogger(__name__)

PROMPTS = {
    "rc": ("✏️ <b>Post #{id}</b> – reply to this message with the changes the submitter should make.",
           "What should be changed?"),
    "ec": ("📝 <b>Post #{id}</b> – reply to this message with the new caption (or new text). "
           "Reply <code>remove</code> for no caption, <code>off</code> for the original.",
           "New caption"),
    "sc": ("🕒 <b>Post #{id}</b> – reply with the publish time: 18:30 · 25-10 18:30 · +90 (minutes) · +2h · off",
           "e.g. 18:30"),
}


def register(app: Application, cfg: Config, svc: PostService, access: Access) -> None:
    open_prompts: dict[int, tuple[str, int]] = {}  # prompt message id -> (action, post id)

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if q.message is None or q.message.chat.id != cfg.approval_group_id:
            await q.answer()  # buttons from any other chat are ignored
            return
        if not access.can_approve(q.from_user.id):
            await q.answer("You don't have permission to approve or reject posts.", show_alert=True)
            return
        parts = (q.data or "").split(":")
        action, raw_id = parts[0], parts[-1]
        try:
            post_id = int(raw_id)
        except ValueError:
            await q.answer()
            return
        if action == "apc":  # approve for one channel / all channels
            channels = [c["id"] for c in await svc.channels()]
            targets = channels if parts[1] == "all" else [int(parts[1])]
            ok, text = await svc.approve(post_id, q.from_user, targets)
            await q.answer(text[:190], show_alert=not ok)
            return
        if action in PROMPTS:
            text, placeholder = PROMPTS[action]
            prompt = await svc.send(cfg.approval_group_id, text.format(id=post_id),
                                    reply_markup=ForceReply(input_field_placeholder=placeholder))
            if prompt is not None:
                open_prompts[prompt.message_id] = (action, post_id)
            await q.answer("Reply to the bot's message below.")
            return
        if action == "pn":
            await q.answer("⚡ Publishing now...")  # answer first: publishing can take a few seconds
            ok, text = await svc.publish_now(post_id, q.from_user)
            if not ok:
                await svc.send(cfg.approval_group_id, text)
            return
        if action == "ap":
            ok, text = await svc.approve(post_id, q.from_user)
        elif action == "rj":
            ok, text = await svc.reject(post_id, q.from_user)
        elif action == "cx":
            ok, text = await svc.cancel(post_id, q.from_user)
        elif action == "tp":
            ok, text = await svc.move_to_top(post_id, q.from_user)
        else:
            await q.answer()
            return
        await q.answer(text[:190], show_alert=not ok)

    async def on_prompt_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if msg is None or user is None or msg.reply_to_message is None:
            return
        key = msg.reply_to_message.message_id
        if key not in open_prompts:
            return
        if not access.can_approve(user.id):
            await msg.reply_text("You don't have permission to do this.")
            return
        action, post_id = open_prompts.pop(key)
        text = msg.text or ""
        if action == "rc":
            _, result = await svc.request_changes(post_id, user, text)
        elif action == "ec":
            word = text.strip().lower()
            value = None if word == "off" else "" if word in ("remove", "none", "hide") else text
            _, result = await svc.set_caption(post_id, user, value)
        else:
            _, result = await svc.schedule_post(post_id, user, text)
        await msg.reply_text(result, parse_mode=ParseMode.HTML)

    app.add_handler(CallbackQueryHandler(
        on_button, pattern=r"^((ap|rj|pn|cx|rc|ec|sc|tp):\d+|apc:(all|-?\d+):\d+)$"))
    app.add_handler(MessageHandler(
        filters.Chat(cfg.approval_group_id) & filters.REPLY & filters.TEXT & ~filters.COMMAND
        & filters.UpdateType.MESSAGE, on_prompt_reply))
