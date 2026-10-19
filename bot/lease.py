"""Single-instance lease in MongoDB: two processes polling one bot token get Telegram 409 Conflict.

Document ``lease/main`` = {holder, host, pid, run, started_at, heartbeat, expires_at}. The lease is free
when it does not exist, is ours, or has expired (no heartbeat for ``expiry_s``). A second instance waits
(polling every ``poll_s``) until the first one releases it or stops heart-beating; it never steals a
live lease. The holder renews every ``heartbeat_s``; if it cannot renew before its own lease would
expire, it calls ``on_lost`` so the process shuts down instead of running beside a new holder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import socket
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from bot.utils import as_utc, utcnow

log = logging.getLogger(__name__)
LEASE_ID = "main"


class Lease:
    def __init__(self, coll: Any, *, expiry_s: float = 120, heartbeat_s: float = 30, poll_s: float = 10,
                 clock: Callable[[], datetime] = utcnow, holder: str | None = None) -> None:
        self.coll = coll
        self.expiry = timedelta(seconds=expiry_s)
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.clock = clock
        run = os.environ.get("GITHUB_RUN_ID")
        suffix = f":run{run}" if run else ""
        self.me = holder or f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}{suffix}"
        self.held = False
        self._task: asyncio.Task | None = None
        self._last_renew: datetime | None = None

    def _fields(self, now: datetime) -> dict:
        return {"holder": self.me, "host": socket.gethostname(), "pid": os.getpid(),
                "run": os.environ.get("GITHUB_RUN_ID"), "heartbeat": now, "expires_at": now + self.expiry}

    async def try_acquire(self) -> bool:
        now = self.clock()
        doc = await self.coll.find_one_and_update(
            {"_id": LEASE_ID, "$or": [{"holder": self.me}, {"expires_at": {"$lt": now}}]},
            {"$set": {**self._fields(now), "started_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            try:
                await self.coll.insert_one({"_id": LEASE_ID, **self._fields(now), "started_at": now})
            except DuplicateKeyError:
                return False
        self.held = True
        self._last_renew = now
        return True

    async def current(self) -> dict | None:
        return await self.coll.find_one({"_id": LEASE_ID})

    async def acquire(self, stop: asyncio.Event | None = None) -> bool:
        """Wait until the lease is ours. Returns False only if ``stop`` is set first."""
        stop = stop or asyncio.Event()
        last_log: datetime | None = None
        while not stop.is_set():
            try:
                if await self.try_acquire():
                    log.info("instance lease acquired (%s)", self.me)
                    return True
                now = self.clock()
                if last_log is None or now - last_log >= timedelta(seconds=60):
                    cur = await self.current() or {}
                    exp = as_utc(cur.get("expires_at"))
                    log.warning("another instance holds the lease (%s, expires %s) - waiting",
                                cur.get("holder"), exp.isoformat() if exp else "?")
                    last_log = now
            except PyMongoError as e:
                log.warning("lease check failed: %s - retrying", e)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
        return False

    async def renew(self) -> bool:
        now = self.clock()
        doc = await self.coll.find_one_and_update(
            {"_id": LEASE_ID, "holder": self.me},
            {"$set": {"heartbeat": now, "expires_at": now + self.expiry}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            self._last_renew = now
        return doc is not None

    def start_heartbeat(self, on_lost: Callable[[str], Any]) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._beat(on_lost), name="lease-heartbeat")

    async def _beat(self, on_lost: Callable[[str], Any]) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                if not await self.renew():
                    self.held = False
                    log.error("instance lease lost (taken by another instance)")
                    on_lost("lease lost")
                    return
            except PyMongoError as e:
                log.warning("lease heartbeat failed: %s", e)
                if self._last_renew and self.clock() - self._last_renew > self.expiry - timedelta(
                        seconds=self.heartbeat_s):
                    log.error("could not renew the lease in time - stopping to avoid two pollers")
                    on_lost("lease heartbeat failing")
                    return

    async def release(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if not self.held:
            return
        try:
            await self.coll.delete_one({"_id": LEASE_ID, "holder": self.me})
            log.info("instance lease released")
        except PyMongoError as e:
            log.warning("could not release lease (it will expire on its own): %s", e)
        self.held = False
