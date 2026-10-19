"""DM submissions: one message = one post; an album (media_group_id) is buffered and becomes ONE post."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from bot import texts
from bot.access import Access
from bot.config import Config
from bot.handlers.access import AccessRequests
from bot.service import PostService

log = logging.getLogger(__name__)


class AlbumBuffer:
    """Collect messages that share a media_group_id; flush ``wait_s`` after the last one arrived."""

    def __init__(self, wait_s: float, on_flush: Callable[[list[Message]], Awaitable[object]]) -> None:
        self.wait_s = wait_s
        self.on_flush = on_flush
        self._groups: dict[tuple[int, str], list[Message]] = {}
        self._timers: dict[tuple[int, str], asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task] = set()

    def add(self, msg: Message) -> None:
        key = (msg.chat_id, str(msg.media_group_id))
        self._groups.setdefault(key, []).append(msg)
        if key in self._timers:
            self._timers[key].cancel()
        self._timers[key] = asyncio.get_running_loop().call_later(self.wait_s, self._fire, key)

    def _fire(self, key: tuple[int, str]) -> None:
        self._timers.pop(key, None)
        self._fire_now(key)

    async def _run(self, msgs: list[Message]) -> None:
        try:
            await self.on_flush(sorted(msgs, key=lambda m: m.message_id))
        except Exception:
            log.exception("album submission failed")

    @property
    def pending(self) -> int:
        return len(self._groups)

    async def flush_all(self) -> None:
        """Shutdown: submit whatever is still buffered and wait for running submissions."""
        for key in list(self._timers):
            self._timers.pop(key).cancel()
            self._fire_now(key)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _fire_now(self, key: tuple[int, str]) -> None:
        msgs = self._groups.pop(key, [])
        if msgs:
            task = asyncio.get_running_loop().create_task(self._run(msgs))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


def register(app: Application, cfg: Config, svc: PostService, albums: AlbumBuffer, access: Access,
             requests: AccessRequests) -> None:
    seen_unauthorized_groups: set[str] = set()

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if msg is None or user is None:
            return
        if not access.can_submit(user.id):
            if msg.media_group_id:
                if msg.media_group_id in seen_unauthorized_groups:
                    return
                seen_unauthorized_groups.add(msg.media_group_id)
            if access.can_approve(user.id):
                await msg.reply_text("ℹ️ Aap approver ho, post bhejne ka access nahi hai. Owner se maango.")
                return
            await requests.request(user)
            await msg.reply_text(texts.unauthorized_text(user.id), parse_mode=ParseMode.HTML)
            return
        if msg.media_group_id:
            albums.add(msg)
            return
        await svc.submit(user, [msg])

    async def on_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.edited_message, update.effective_user
        if msg is None or user is None or not access.can_submit(user.id):
            return
        await svc.handle_edit(msg)

    private = filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(
        private & filters.UpdateType.MESSAGE & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_message))
    app.add_handler(MessageHandler(private & filters.UpdateType.EDITED_MESSAGE, on_edit))
