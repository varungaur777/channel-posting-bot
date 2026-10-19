"""Shared fixtures: config, both DB backends (JSON + mongomock-motor), a fake Telegram bot, a clock."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mongomock_motor import AsyncMongoMockClient
from telegram import Chat, Message, PhotoSize, User
from telegram.error import BadRequest

from bot.config import Config
from bot.db import Database
from bot.jsonstore import JsonStore
from bot.service import PostService

OWNER, ADMIN, APPROVER, STRANGER = 1, 2, 3, 99
APPROVAL, STORAGE, CHANNEL = -1001, -1002, -1003


def make_config(**over: Any) -> Config:
    base = dict(
        bot_token="123:TEST", owner_id=OWNER, submitter_ids=frozenset({ADMIN}), approver_ids=frozenset({APPROVER}),
        approval_group_id=APPROVAL, storage_channel_id=STORAGE, channel_id=CHANNEL, mongo_url="",
        mongo_db_name="test", min_interval_min=10, max_interval_min=15, tz_name="Asia/Kolkata",
        dup_window_days=30, max_runtime_minutes=0, runtime_profile="dev", notify_restart=False,
        data_dir=Path("data"), log_dir=Path("logs"), log_level="INFO", file_logging=False,
    )
    base.update(over)
    return Config(**base)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class FakeBot:
    """Records every call; chats are dicts of message_id -> content. Failures can be queued per method."""

    def __init__(self) -> None:
        self.chats: dict[int, dict[int, Any]] = defaultdict(dict)
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list[BaseException]] = defaultdict(list)
        self.hang: set[str] = set()
        self._next = 1000

    def _id(self) -> int:
        self._next += 1
        return self._next

    async def _hook(self, name: str, kwargs: dict) -> None:
        self.calls.append((name, kwargs))
        if name in self.hang:
            import asyncio

            await asyncio.sleep(3600)
        if self.fail[name]:
            raise self.fail[name].pop(0)

    def put(self, chat_id: int, content: Any) -> int:
        mid = self._id()
        self.chats[chat_id][mid] = content
        return mid

    def sent_to(self, chat_id: int) -> list[Any]:
        return list(self.chats[chat_id].values())

    def texts_to(self, chat_id: int) -> list[str]:
        return [c["text"] for c in self.chats[chat_id].values() if isinstance(c, dict) and "text" in c]

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int, **kw: Any) -> Any:
        await self._hook("copy_message", dict(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id))
        src = self.chats[from_chat_id]
        if message_id not in src:
            raise BadRequest("Message to copy not found")
        return SimpleNamespace(message_id=self.put(chat_id, src[message_id]))

    async def copy_messages(self, chat_id: int, from_chat_id: int, message_ids: list[int], **kw: Any) -> Any:
        await self._hook("copy_messages", dict(chat_id=chat_id, from_chat_id=from_chat_id, message_ids=message_ids))
        src = self.chats[from_chat_id]
        return tuple(SimpleNamespace(message_id=self.put(chat_id, src[m])) for m in message_ids if m in src)

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> Any:
        await self._hook("send_message", dict(chat_id=chat_id, text=text, **kw))
        return SimpleNamespace(message_id=self.put(chat_id, {"text": text, **kw}))

    async def edit_message_text(self, text: str, chat_id: int, message_id: int, **kw: Any) -> Any:
        await self._hook("edit_message_text", dict(chat_id=chat_id, message_id=message_id, text=text, **kw))
        self.chats[chat_id][message_id] = {"text": text, **kw}
        return True

    async def delete_messages(self, chat_id: int, message_ids: list[int], **kw: Any) -> bool:
        await self._hook("delete_messages", dict(chat_id=chat_id, message_ids=message_ids))
        for m in message_ids:
            self.chats[chat_id].pop(m, None)
        return True

    async def send_media_group(self, chat_id: int, media: list[Any], **kw: Any) -> Any:
        await self._hook("send_media_group", dict(chat_id=chat_id, media=media))
        return tuple(SimpleNamespace(message_id=self.put(chat_id, {"media": m.media})) for m in media)

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("send_"):
            raise AttributeError(name)

        async def sender(chat_id: int, **kw: Any) -> Any:
            await self._hook(name, dict(chat_id=chat_id, **kw))
            return SimpleNamespace(message_id=self.put(chat_id, {name: kw}))

        return sender


def user(uid: int, name: str = "User") -> User:
    return User(id=uid, first_name=f"{name}{uid}", is_bot=False)


_mid = iter(range(1, 10**6))


def text_msg(uid: int, text: str, bot: FakeBot | None = None) -> Message:
    m = Message(message_id=next(_mid), date=datetime.now(UTC), chat=Chat(id=uid, type="private"),
                from_user=user(uid), text=text)
    if bot is not None:  # make it exist in the user's DM so it can be copied
        bot.chats[uid][m.message_id] = {"text": text}
    return m


def photo_msg(uid: int, unique: str, caption: str | None = None, group: str | None = None,
              bot: FakeBot | None = None) -> Message:
    photo = (PhotoSize(file_id=f"fid-{unique}", file_unique_id=unique, width=10, height=10, file_size=123),)
    m = Message(message_id=next(_mid), date=datetime.now(UTC), chat=Chat(id=uid, type="private"),
                from_user=user(uid), photo=photo, caption=caption, media_group_id=group)
    if bot is not None:
        bot.chats[uid][m.message_id] = {"photo": unique, "caption": caption}
    return m


@pytest.fixture(params=["json", "mongo"])
def db(request: pytest.FixtureRequest) -> Database:
    if request.param == "json":
        return Database(JsonStore(None), backend="json")
    return Database(AsyncMongoMockClient(tz_aware=True)["test"], backend="mongo")


@pytest.fixture
def mongo_db() -> Database:
    return Database(AsyncMongoMockClient(tz_aware=True)["test"], backend="mongo")


@pytest.fixture
def cfg() -> Config:
    return make_config()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def svc(cfg: Config, db: Database, bot: FakeBot, clock: Clock) -> PostService:
    return PostService(cfg, db, bot, clock=clock, rng=random.Random(42))
