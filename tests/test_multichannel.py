"""Posting to more than one channel."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import NetworkError

from bot.access import Access
from bot.db import APPROVED, PUBLISHED
from bot.handlers import approval
from tests.conftest import ADMIN, APPROVAL, APPROVER, CHANNEL, photo_msg, text_msg, user

CH2 = -1009


async def two_channels(svc) -> None:
    await svc.load_channel_info()
    await svc.db.set_state(extra_channels=[{"id": CH2, "title": "The GeoSight", "username": "GeoSight"}])


def sent_text(bot, chat):
    return [c for c in bot.sent_to(chat) if isinstance(c, dict) and "text" in c]


async def test_approve_both_publishes_to_both(svc, bot, db) -> None:
    await two_channels(svc)
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "news", bot)])
    ok, text = await svc.approve(1, user(APPROVER), [CHANNEL, CH2])
    assert ok and "My Channel + The GeoSight" in text
    assert await svc.tick() == 1
    assert sent_text(bot, CHANNEL) == [{"text": "news"}] and sent_text(bot, CH2) == [{"text": "news"}]
    post = await db.get_post(1)
    assert set(post["published_to"]) == {str(CHANNEL), str(CH2)} and post["channel_message_ids"]
    assert any("published to My Channel + The GeoSight" in t for t in bot.texts_to(ADMIN))


async def test_single_channel_choice_and_default(svc, bot, db, clock) -> None:
    await two_channels(svc)
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "only geo", bot)])
    await svc.approve(1, user(APPROVER), [CH2])
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "default main", bot)])
    await svc.approve(2, user(APPROVER))  # no choice -> main channel
    await svc.tick()
    clock.now = (await db.get_state())["next_publish_at"]
    await svc.tick()
    assert sent_text(bot, CH2) == [{"text": "only geo"}]
    assert sent_text(bot, CHANNEL) == [{"text": "default main"}]
    ok, _ = await svc.approve(1, user(APPROVER), [CH2])  # already decided
    assert not ok


async def test_unknown_target_falls_back_to_main(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "x", bot)])
    await svc.approve(1, user(APPROVER), [-555])
    assert (await db.get_post(1))["targets"] == [CHANNEL]


async def test_partial_failure_retries_only_missing_channel(svc, bot, db, clock) -> None:
    await two_channels(svc)
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "once each", bot)])
    await svc.approve(1, user(APPROVER), [CHANNEL, CH2])

    real_copy = bot.copy_message

    async def flaky(chat_id, from_chat_id, message_id, **kw):
        if chat_id == CH2 and not getattr(flaky, "done", False):
            flaky.done = True
            raise NetworkError("blip")
        return await real_copy(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kw)

    bot.copy_message = flaky
    assert await svc.tick() is None  # main ok, GeoSight failed -> backoff
    post = await db.get_post(1)
    assert post["status"] == APPROVED and list(post["published_to"]) == [str(CHANNEL)]
    clock.now = post["next_attempt_at"]
    assert await svc.tick() == 1
    assert len(sent_text(bot, CHANNEL)) == 1 and len(sent_text(bot, CH2)) == 1  # nobody got it twice
    assert (await db.get_post(1))["status"] == PUBLISHED


async def test_edit_after_publish_edits_every_channel(svc, bot, db) -> None:
    await two_channels(svc)
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "old", bot=bot)])
    await svc.approve(1, user(APPROVER), [CHANNEL, CH2])
    await svc.tick()
    ok, _ = await svc.edit_post(1, user(ADMIN), "new caption", moderator=False)
    assert ok
    edited = {c["chat_id"]: c["caption"] for n, c in bot.calls if n == "edit_message_caption"}
    assert edited == {CHANNEL: "new caption", CH2: "new caption"}


async def test_reply_goes_to_the_right_message_in_each_channel(svc, bot, db, clock) -> None:
    await two_channels(svc)
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "story", bot)])
    await svc.approve(1, user(APPROVER), [CHANNEL, CH2])
    await svc.tick()
    where = (await db.get_post(1))["published_to"]
    await svc.arm_reply(user(ADMIN), "1")
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "update", bot)])
    await svc.approve(2, user(APPROVER), [CHANNEL, CH2])
    clock.now = (await db.get_state())["next_publish_at"]
    await svc.tick()
    replies = {c["chat_id"]: c["reply_parameters"].message_id
               for n, c in bot.calls if n == "copy_message" and c.get("reply_parameters")}
    assert replies == {CHANNEL: where[str(CHANNEL)][0], CH2: where[str(CH2)][0]}


async def test_link_reply_only_in_its_own_channel(svc, bot, db) -> None:
    await two_channels(svc)
    await svc.arm_reply(user(ADMIN), "https://t.me/GeoSight/55")
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "reply", bot)])
    await svc.approve(1, user(APPROVER), [CHANNEL, CH2])
    await svc.tick()
    replies = {c["chat_id"]: c.get("reply_parameters") for n, c in bot.calls
               if n == "copy_message" and c["chat_id"] in (CHANNEL, CH2)}
    assert replies[CHANNEL] is None and replies[CH2].message_id == 55


class FakeApp:
    def __init__(self) -> None:
        self.handlers = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


async def test_channel_buttons_in_approval_group(cfg, svc, bot, db) -> None:
    await two_channels(svc)
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "pick", bot)])
    control = bot.chats[APPROVAL][(await db.get_post(1))["control_message_id"]]
    data = [b.callback_data for row in control["reply_markup"].inline_keyboard for b in row]
    assert data[:3] == [f"apc:{CHANNEL}:1", f"apc:{CH2}:1", "apc:all:1"]

    acc = Access(cfg, db)
    await acc.load()
    app = FakeApp()
    approval.register(app, cfg, svc, acc)
    q = SimpleNamespace(data="apc:all:1", from_user=user(APPROVER), answer=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=APPROVAL)))
    await app.handlers[0].callback(SimpleNamespace(callback_query=q), None)
    assert (await db.get_post(1))["targets"] == [CHANNEL, CH2]
    control = bot.chats[APPROVAL][(await db.get_post(1))["control_message_id"]]
    assert "📢 Channels: My Channel + The GeoSight" in control["text"]


async def test_one_channel_keeps_the_old_buttons(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "single", bot)])
    control = bot.chats[APPROVAL][(await db.get_post(1))["control_message_id"]]
    data = [b.callback_data for row in control["reply_markup"].inline_keyboard for b in row]
    assert data[0] == "ap:1" and not any(d.startswith("apc") for d in data)


async def test_add_and_remove_channel(svc, bot, db) -> None:
    await svc.load_channel_info()
    admin = SimpleNamespace(status="administrator", can_post_messages=True)
    bot.get_me = AsyncMock(return_value=SimpleNamespace(id=777))
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(id=CH2, type="channel", title="The GeoSight",
                                                          username="GeoSight"))
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member", can_post_messages=False))
    ok, text = await svc.add_channel("@GeoSight")
    assert not ok and "not an admin" in text
    bot.get_chat_member = AsyncMock(return_value=admin)
    ok, _ = await svc.add_channel("@GeoSight")
    assert ok and [c["id"] for c in await svc.channels()] == [CHANNEL, CH2]
    assert "The GeoSight" in await svc.channels_text()
    assert (await svc.resolve_channel("@geosight"))["id"] == CH2
    ok, _ = await svc.remove_channel("@GeoSight")
    assert ok and [c["id"] for c in await svc.channels()] == [CHANNEL]
    ok, _ = await svc.remove_channel(str(CHANNEL))
    assert not ok  # the main channel stays
