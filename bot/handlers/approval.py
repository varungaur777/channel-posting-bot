"""Buttons on control messages in the approval group.

Pending post:   ✅ Approve · ❌ Reject · ⚡ Approve & Post Now
Scheduled post: ⚡ Post Now · 🚫 Cancel
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot.access import Access
from bot.config import Config
from bot.service import PostService

log = logging.getLogger(__name__)


def register(app: Application, cfg: Config, svc: PostService, access: Access) -> None:
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
        action, _, raw_id = (q.data or "").partition(":")
        try:
            post_id = int(raw_id)
        except ValueError:
            await q.answer()
            return
        if action == "ap":
            ok, text = await svc.approve(post_id, q.from_user)
        elif action == "rj":
            ok, text = await svc.reject(post_id, q.from_user)
        elif action == "pn":
            await q.answer("⚡ Publishing now...")  # answer first: publishing can take a few seconds
            ok, text = await svc.publish_now(post_id, q.from_user)
            if not ok:
                await svc.send(cfg.approval_group_id, text)
            return
        elif action == "cx":
            ok, text = await svc.cancel(post_id, q.from_user)
        else:
            await q.answer()
            return
        await q.answer(text[:190], show_alert=not ok)

    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(ap|rj|pn|cx):\d+$"))
