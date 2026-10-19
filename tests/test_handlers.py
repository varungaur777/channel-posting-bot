"""Handler-level permission checks for the approval buttons (Telegram objects mocked)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.access import Access
from bot.db import APPROVED, PENDING
from bot.handlers import access as access_handlers
from bot.handlers import approval
from bot.handlers.access import AccessRequests
from tests.conftest import ADMIN, APPROVAL, APPROVER, OWNER, STRANGER, text_msg, user


class FakeApp:
    def __init__(self) -> None:
        self.handlers = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


def click(data: str, uid: int, chat_id: int = APPROVAL):
    q = SimpleNamespace(data=data, from_user=user(uid), message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
                        answer=AsyncMock())
    return SimpleNamespace(callback_query=q), q


@pytest.fixture
async def access(cfg, db):
    acc = Access(cfg, db)
    await acc.load()
    return acc


@pytest.fixture
async def button(cfg, svc, bot, access):
    app = FakeApp()
    approval.register(app, cfg, svc, access)
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "hello", bot)])
    return app.handlers[0].callback


async def test_stranger_cannot_approve(button, db) -> None:
    upd, q = click("ap:1", STRANGER)
    await button(upd, None)
    assert q.answer.await_args.kwargs.get("show_alert") is True
    assert (await db.get_post(1))["status"] == PENDING


async def test_submitter_only_cannot_approve(button, db) -> None:
    upd, _ = click("ap:1", ADMIN)
    await button(upd, None)
    assert (await db.get_post(1))["status"] == PENDING


async def test_click_from_other_chat_ignored(button, db) -> None:
    upd, q = click("ap:1", OWNER, chat_id=-555)
    await button(upd, None)
    q.answer.assert_awaited_once_with()
    assert (await db.get_post(1))["status"] == PENDING


async def test_approver_click_and_double_click(button, db) -> None:
    upd, _ = click("ap:1", APPROVER)
    await button(upd, None)
    assert (await db.get_post(1))["status"] == APPROVED
    upd2, q2 = click("rj:1", OWNER)
    await button(upd2, None)
    assert q2.answer.await_args.kwargs.get("show_alert") is True  # already decided
    assert (await db.get_post(1))["status"] == APPROVED


async def test_user_added_from_bot_can_approve(button, access, db) -> None:
    await access.grant(STRANGER, ("approve",))
    upd, _ = click("ap:1", STRANGER)
    await button(upd, None)
    assert (await db.get_post(1))["status"] == APPROVED


# ---- access store + access requests -------------------------------------------------------------


async def test_access_grant_revoke_persists(cfg, db) -> None:
    acc = Access(cfg, db)
    await acc.load()
    assert not acc.can_submit(STRANGER)
    assert await acc.grant(STRANGER, ("post", "approve")) == ["post", "approve"]
    assert await acc.grant(STRANGER, ("post",)) == []  # already has it
    again = Access(cfg, db)  # simulates a restart
    await again.load()
    assert again.can_submit(STRANGER) and again.can_approve(STRANGER)
    removed, from_env = await again.revoke(STRANGER, ("post",))
    assert removed == ["post"] and from_env == []
    assert not again.can_submit(STRANGER) and again.can_approve(STRANGER)
    removed, from_env = await again.revoke(ADMIN, ("post",))  # ADMIN comes from SUBMITTER_IDS env
    assert removed == [] and from_env == ["post"] and again.can_submit(ADMIN)


def owner_click(data: str, uid: int = OWNER, chat_type: str = "private"):
    q = SimpleNamespace(data=data, from_user=user(uid), answer=AsyncMock(), edit_message_text=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=uid, type=chat_type), text_html="req"))
    return SimpleNamespace(callback_query=q), q


async def test_access_request_flow(cfg, svc, bot, access) -> None:
    reqs = AccessRequests(cfg, svc, access)
    assert await reqs.request(user(STRANGER)) is True
    assert await reqs.request(user(STRANGER)) is False  # only once
    owner_msg = bot.chats[OWNER][max(bot.chats[OWNER])]
    assert str(STRANGER) in owner_msg["text"] and owner_msg["reply_markup"] is not None

    app = FakeApp()
    access_handlers.register(app, cfg, svc, access)
    on_button = app.handlers[0].callback

    upd, q = owner_click(f"acc:post:{STRANGER}", uid=APPROVER)  # not the owner
    await on_button(upd, None)
    assert not access.can_submit(STRANGER) and q.answer.await_args.kwargs.get("show_alert") is True

    upd, q = owner_click(f"acc:post:{STRANGER}")
    await on_button(upd, None)
    assert access.can_submit(STRANGER) and not access.can_approve(STRANGER)
    assert any("You have been given access" in t for t in bot.texts_to(STRANGER))
    q.edit_message_text.assert_awaited()


async def test_ignore_request_grants_nothing(cfg, svc, bot, access) -> None:
    app = FakeApp()
    access_handlers.register(app, cfg, svc, access)
    upd, _ = owner_click(f"acc:no:{STRANGER}")
    await app.handlers[0].callback(upd, None)
    assert not access.is_staff(STRANGER)


async def test_post_now_button(button, bot, db) -> None:
    upd, q = click("pn:1", APPROVER)
    await button(upd, None)
    assert (await db.get_post(1))["status"] == "published"
    upd, q = click("pn:1", STRANGER)
    await button(upd, None)
    assert q.answer.await_args.kwargs.get("show_alert") is True  # not an approver
