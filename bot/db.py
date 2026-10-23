"""MongoDB (Motor) access, with a JSON-file fallback for local development.

Collections
-----------
posts     one document per post, ``_id`` = sequential number (#1, #2, ...)
meta      one document ``_id="state"``: next_publish_at, last_published_at, paused, min/max interval
counters  ``_id="posts"`` -> ``seq`` (atomic $inc)
lease     ``_id="main"``: which process is allowed to poll Telegram (see lease.py)

All status changes go through :meth:`Database.transition`, a ``find_one_and_update`` filtered on the
expected current status, so two clicks / two processes can never both win.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument

from bot.config import Config
from bot.jsonstore import JsonStore
from bot.utils import as_utc, utcnow

log = logging.getLogger(__name__)

PENDING, APPROVED, PUBLISHING, PUBLISHED = "pending", "approved", "publishing", "published"
REJECTED, CANCELLED, FAILED, NEEDS_REVIEW = "rejected", "cancelled", "failed", "needs_review"
CHANGES_REQUESTED = "changes_requested"
STATUSES = (PENDING, APPROVED, PUBLISHING, PUBLISHED, REJECTED, CHANGES_REQUESTED, CANCELLED, FAILED, NEEDS_REVIEW)
DUP_STATUSES = (PENDING, APPROVED, PUBLISHING, PUBLISHED, NEEDS_REVIEW)
_DATE_FIELDS = ("created_at", "decided_at", "publishing_started_at", "published_at", "next_attempt_at",
                "updated_at", "scheduled_for", "reminded_at")


def _fix_dates(doc: dict | None) -> dict | None:
    """Make every datetime timezone-aware UTC whatever the driver returned."""
    if doc is None:
        return None
    for key, value in doc.items():
        if isinstance(value, datetime):
            doc[key] = as_utc(value)
    for ev in doc.get("history") or ():
        if isinstance(ev, dict) and isinstance(ev.get("at"), datetime):
            ev["at"] = as_utc(ev["at"])
    return doc


class Database:
    def __init__(self, raw_db: Any, *, backend: str, client: Any = None) -> None:
        self.raw = raw_db
        self.backend = backend  # "mongo" | "json"
        self._client = client

    # ---- lifecycle -----------------------------------------------------------------------------
    @classmethod
    async def connect(cls, cfg: Config) -> Database:
        if not cfg.mongo_url:
            path = cfg.data_dir / "db.json"
            log.warning("MONGO_URL is empty - using local JSON store %s (development only)", path)
            return cls(JsonStore(path), backend="json")
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(cfg.mongo_url, tz_aware=True, serverSelectionTimeoutMS=20_000,
                                    appname="channel-posting-bot")
        await client.admin.command("ping")
        log.info("connected to MongoDB, database %r", cfg.mongo_db_name)
        return cls(client[cfg.mongo_db_name], backend="mongo", client=client)

    @property
    def is_mongo(self) -> bool:
        return self.backend == "mongo"

    @property
    def posts(self) -> Any:
        return self.raw["posts"]

    @property
    def meta(self) -> Any:
        return self.raw["meta"]

    @property
    def counters(self) -> Any:
        return self.raw["counters"]

    @property
    def lease(self) -> Any:
        return self.raw["lease"]

    async def ensure_indexes(self) -> None:
        await self.posts.create_index([("status", ASCENDING), ("decided_at", ASCENDING)])
        await self.posts.create_index([("content_hash", ASCENDING)])
        await self.posts.create_index([("created_at", DESCENDING)])

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # ---- posts ---------------------------------------------------------------------------------
    async def next_post_id(self) -> int:
        doc = await self.counters.find_one_and_update(
            {"_id": "posts"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
        )
        return int(doc["seq"])

    async def insert_post(self, doc: dict) -> None:
        await self.posts.insert_one(doc)

    async def get_post(self, post_id: int) -> dict | None:
        return _fix_dates(await self.posts.find_one({"_id": post_id}))

    async def update_post(self, post_id: int, fields: dict) -> dict | None:
        fields = {**fields, "updated_at": utcnow()}
        return _fix_dates(await self.posts.find_one_and_update(
            {"_id": post_id}, {"$set": fields}, return_document=ReturnDocument.AFTER))

    async def transition(
        self,
        post_id: int,
        from_statuses: tuple[str, ...] | list[str],
        to_status: str,
        fields: dict | None = None,
        *,
        event: str | None = None,
        by: int | None = None,
        inc: dict | None = None,
    ) -> dict | None:
        """Atomically move a post to ``to_status`` only if it is currently in ``from_statuses``.

        Returns the updated document, or ``None`` when the post is missing / in another status.
        """
        now = utcnow()
        update: dict[str, Any] = {"$set": {**(fields or {}), "status": to_status, "updated_at": now}}
        update["$push"] = {"history": {"at": now, "event": event or to_status, "by": by}}
        if inc:
            update["$inc"] = inc
        doc = await self.posts.find_one_and_update(
            {"_id": post_id, "status": {"$in": list(from_statuses)}}, update,
            return_document=ReturnDocument.AFTER,
        )
        return _fix_dates(doc)

    async def find_duplicate(self, content_hash: str, since: datetime) -> dict | None:
        return _fix_dates(await self.posts.find_one(
            {"content_hash": content_hash, "status": {"$in": list(DUP_STATUSES)}, "created_at": {"$gte": since}},
            sort=[("_id", DESCENDING)],
        ))

    async def find_by_source(self, chat_id: int, message_id: int) -> dict | None:
        return _fix_dates(await self.posts.find_one({"source_chat_id": chat_id, "source_message_ids": message_id}))

    async def list_posts(self, statuses: list[str] | None, limit: int) -> list[dict]:
        flt = {"status": {"$in": statuses}} if statuses else {}
        docs = await self.posts.find(flt).sort("_id", DESCENDING).limit(limit).to_list(limit)
        return [_fix_dates(d) for d in docs]

    async def posts_with_status(self, status: str) -> list[dict]:
        docs = await self.posts.find({"status": status}).sort("_id", ASCENDING).to_list(None)
        return [_fix_dates(d) for d in docs]

    async def queue(self) -> list[dict]:
        """Approved ("scheduled") posts in publish order: approval time, then post id."""
        docs = await self.posts.find({"status": APPROVED}).sort(
            [("decided_at", ASCENDING), ("_id", ASCENDING)]).to_list(None)
        return [_fix_dates(d) for d in docs]

    async def next_due(self, now: datetime) -> dict | None:
        """First approved post that is not waiting for a retry backoff."""
        flt = {"status": APPROVED, "scheduled_for": None,
               "$or": [{"next_attempt_at": None}, {"next_attempt_at": {"$lte": now}}]}
        docs = await self.posts.find(flt).sort([("decided_at", ASCENDING), ("_id", ASCENDING)]).limit(1).to_list(1)
        return _fix_dates(docs[0]) if docs else None

    async def next_due_for(self, channel: int, now: datetime, *, is_main: bool) -> dict | None:
        """First approved post that still has to go to ``channel`` and is not waiting for a retry backoff.

        Posts without a ``targets`` list (approved before multi-channel support) belong to the main channel.
        """
        for_channel: list[dict] = [{"targets": channel}]
        if is_main:
            for_channel += [{"targets": None}, {"targets": []}]
        flt = {
            "status": APPROVED, "scheduled_for": None, f"published_to.{channel}": {"$exists": False},
            "$and": [{"$or": [{"next_attempt_at": None}, {"next_attempt_at": {"$lte": now}}]},
                     {"$or": for_channel}],
        }
        docs = await self.posts.find(flt).sort([("decided_at", ASCENDING), ("_id", ASCENDING)]).limit(1).to_list(1)
        return _fix_dates(docs[0]) if docs else None

    async def next_timed(self, now: datetime) -> dict | None:
        """First approved post whose fixed publish time (/schedule) has come."""
        flt = {"status": APPROVED, "scheduled_for": {"$lte": now},
               "$or": [{"next_attempt_at": None}, {"next_attempt_at": {"$lte": now}}]}
        docs = await self.posts.find(flt).sort([("scheduled_for", ASCENDING), ("_id", ASCENDING)]).limit(1).to_list(1)
        return _fix_dates(docs[0]) if docs else None

    async def posts_where(self, flt: dict) -> list[dict]:
        docs = await self.posts.find(flt).sort("_id", ASCENDING).to_list(None)
        return [_fix_dates(d) for d in docs]

    async def count(self, flt: dict) -> int:
        return await self.posts.count_documents(flt)

    async def count_by_status(self) -> dict[str, int]:
        return {s: await self.posts.count_documents({"status": s}) for s in STATUSES}

    # ---- meta ----------------------------------------------------------------------------------
    async def get_state(self) -> dict:
        return _fix_dates(await self.meta.find_one({"_id": "state"})) or {"_id": "state"}

    async def set_state(self, **fields: Any) -> dict:
        doc = await self.meta.find_one_and_update(
            {"_id": "state"}, {"$set": fields}, upsert=True, return_document=ReturnDocument.AFTER)
        return _fix_dates(doc)
