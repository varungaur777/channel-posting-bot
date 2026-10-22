"""AI caption helper: off without a key, suggestions via a fake Claude client, error handling, DM flow."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest

from bot.access import Access
from bot.ai import AIUnavailable, CaptionAI
from bot.handlers import dm
from bot.service import PostService
from tests.conftest import ADMIN, STRANGER, photo_msg, text_msg, user


def fake_response(options=None, stop_reason="end_turn", raw=None):
    text = raw if raw is not None else json.dumps({"options": options or []})
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def ai_with(create: AsyncMock) -> CaptionAI:
    ai = CaptionAI("test-key")
    ai.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    return ai


def test_off_without_key() -> None:
    assert not CaptionAI("").enabled
    assert CaptionAI("k").enabled and CaptionAI("k").model == "claude-opus-5-5"
    assert CaptionAI("k", "claude-haiku-5-5").model == "claude-haiku-5-5"


async def test_suggest_request_and_cleanup() -> None:
    create = AsyncMock(return_value=fake_response(["A", "B", "B", "", "C", "D"]))
    out = await ai_with(create).suggest("old caption", "improve", limit=1024)
    assert out == ["A", "B", "C"]  # deduped, empties dropped, max 3
    kw = create.await_args.kwargs
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "old caption" in kw["messages"][0]["content"]


async def test_suggest_errors() -> None:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    with pytest.raises(AIUnavailable, match="declined"):
        await ai_with(AsyncMock(return_value=fake_response(stop_reason="refusal"))).suggest("x", "improve")
    with pytest.raises(AIUnavailable, match="unexpected"):
        await ai_with(AsyncMock(return_value=fake_response(raw="not json"))).suggest("x", "improve")
    with pytest.raises(AIUnavailable, match="not valid"):
        err = anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=req), body=None)
        await ai_with(AsyncMock(side_effect=err)).suggest("x", "improve")
    with pytest.raises(AIUnavailable, match="reach"):
        await ai_with(AsyncMock(side_effect=anthropic.APIConnectionError(request=req))).suggest("x", "improve")
    with pytest.raises(AIUnavailable, match="off"):
        await CaptionAI("").suggest("x", "improve")


async def test_no_ai_buttons_when_off(svc, bot) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "hello", bot)])
    last = bot.chats[ADMIN][max(bot.chats[ADMIN])]
    data = [b.callback_data for row in last["reply_markup"].inline_keyboard for b in row]
    assert not any(d.startswith("dmai") for d in data)
    options, msg = await svc.ai_suggest(1, user(ADMIN), "improve", moderator=False)
    assert options == [] and "off" in msg


class FakeApp:
    def __init__(self) -> None:
        self.handlers = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


def click(data: str, uid: int = ADMIN):
    q = SimpleNamespace(data=data, from_user=user(uid), answer=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=uid, type="private")))
    return SimpleNamespace(callback_query=q), q


async def test_ai_dm_flow(cfg, db, bot, clock) -> None:
    create = AsyncMock(return_value=fake_response(["🔥 Better caption", "Short one"]))
    svc = PostService(cfg, db, bot, clock=clock, ai=ai_with(create))
    acc = Access(cfg, db)
    await acc.load()
    app = FakeApp()
    dm.register(app, cfg, svc, acc)
    on_button = app.handlers[0].callback

    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "my caption", bot=bot)])
    last = bot.chats[ADMIN][max(bot.chats[ADMIN])]
    assert [b.callback_data for b in last["reply_markup"].inline_keyboard[0]] == \
        ["dmai:improve:1", "dmai:translate:1", "dmai:hashtags:1"]

    await on_button(click("dmai:improve:1")[0], None)
    suggestions = bot.chats[ADMIN][max(bot.chats[ADMIN])]
    assert "🔥 Better caption" in suggestions["text"] and "Short one" in suggestions["text"]
    assert "my caption" in create.await_args.kwargs["messages"][0]["content"]

    await on_button(click("dmu:0:1")[0], None)
    assert (await db.get_post(1))["caption_override"] == "🔥 Better caption"
    upd, q = click("dmu:1:1")  # suggestions are used once
    await on_button(upd, None)
    assert q.answer.await_args.kwargs.get("show_alert") is True

    await on_button(click("dmai:improve:1", uid=STRANGER)[0], None)  # not their post
    assert create.await_count == 1


async def test_ai_needs_a_caption(cfg, db, bot, clock) -> None:
    svc = PostService(cfg, db, bot, clock=clock, ai=ai_with(AsyncMock()))
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", None, bot=bot)])
    options, msg = await svc.ai_suggest(1, user(ADMIN), "improve", moderator=False)
    assert options == [] and "no caption" in msg
