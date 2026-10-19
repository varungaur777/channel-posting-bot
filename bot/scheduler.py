"""Publishing loop: every ``tick_seconds`` ask the service to publish the next due post.

Runs as its own asyncio task (not the PTB JobQueue) so shutdown can wait for an in-flight publish and,
if it does not finish in time, cancel it - the service then marks that post ``needs_review``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from bot.service import PostService

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, service: PostService, tick_seconds: float = 20.0) -> None:
        self.service = service
        self.tick_seconds = tick_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="scheduler")
            log.info("scheduler started (tick every %ss)", self.tick_seconds)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.service.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)

    async def stop(self, grace_s: float = 20.0) -> None:
        """Stop ticking; give an in-flight publish ``grace_s`` to finish, then cancel it."""
        self._stop.set()
        if self._task is None:
            return
        done, _ = await asyncio.wait({self._task}, timeout=grace_s)
        if not done:
            log.warning("publish still running after %ss - cancelling (post becomes needs_review)", grace_s)
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        log.info("scheduler stopped")
