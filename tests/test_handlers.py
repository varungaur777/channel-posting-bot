"""Handler-level permission checks for the approval buttons (Telegram objects mocked)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.db import APPROVED, PENDING
from bot.handlers import approval
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
async def button(cfg, svc, bot):
    app = FakeApp()
    approval.register(app, cfg, svc)
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
