"""Reply posts, removing captions, clearing buttons from channel posts."""

from __future__ import annotations

from bot.db import PUBLISHED
from tests.conftest import ADMIN, APPROVER, CHANNEL, STRANGER, photo_msg, text_msg, user


async def approved(svc, bot, msg) -> int:
    pid = await svc.submit(user(ADMIN), [msg])
    await svc.approve(pid, user(APPROVER))
    return pid


def reply_id(call) -> int | None:
    rp = call.get("reply_parameters")
    return rp.message_id if rp else None


# ---- reply posts ------------------------------------------------------------------------------------


async def test_next_post_replies_to_published_post(svc, bot, db, clock) -> None:
    await approved(svc, bot, text_msg(ADMIN, "main story", bot))
    await svc.tick()
    target_msg = (await db.get_post(1))["channel_message_ids"][0]
    ok, text = await svc.arm_reply(user(ADMIN), "1")
    assert ok and "reply to Post #1" in text
    pid = await approved(svc, bot, text_msg(ADMIN, "update to the story", bot))
    assert (await db.get_post(pid))["reply_to_post"] == 1
    assert any("reply to Post #1" in t for t in bot.texts_to(ADMIN))
    clock.advance(minutes=20)
    assert await svc.tick() == pid
    call = [c for n, c in bot.calls if n == "copy_message" and c["chat_id"] == CHANNEL][-1]
    assert reply_id(call) == target_msg
    # reply mode is used once
    pid3 = await approved(svc, bot, text_msg(ADMIN, "unrelated", bot))
    assert (await db.get_post(pid3))["reply_to_post"] is None


async def test_reply_by_channel_link_and_album(svc, bot, db) -> None:
    await svc.load_channel_info()  # main channel is @mychannel in the fake bot
    ok, _ = await svc.arm_reply(user(ADMIN), "https://t.me/mychannel/777")
    assert ok
    msgs = [photo_msg(ADMIN, u, group="g", bot=bot) for u in ("A", "B")]
    await svc.submit(user(ADMIN), msgs)
    await svc.approve(1, user(APPROVER))
    assert await svc.tick() == 1
    album = next(c for n, c in bot.calls if n == "send_media_group")  # copyMessages cannot reply -> rebuilt
    assert reply_id(album) == 777


async def test_reply_target_not_published_posts_normally(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "still pending", bot)])
    ok, _ = await svc.arm_reply(user(ADMIN), "1")
    assert ok
    pid = await approved(svc, bot, text_msg(ADMIN, "reply", bot))
    assert await svc.tick() == pid
    call = next(c for n, c in bot.calls if n == "copy_message" and c["chat_id"] == CHANNEL)
    assert reply_id(call) is None


async def test_reply_errors_and_set_reply_on_waiting_post(svc, bot, db) -> None:
    for bad in ("99", "abc"):
        ok, _ = await svc.arm_reply(user(ADMIN), bad)
        assert not ok
    ok, _ = await svc.arm_reply(user(ADMIN), "off")
    assert ok and ADMIN not in svc.reply_targets
    await approved(svc, bot, text_msg(ADMIN, "a", bot))
    await approved(svc, bot, text_msg(ADMIN, "b", bot))
    ok, _ = await svc.set_reply(2, user(STRANGER), "1", moderator=False)
    assert not ok
    ok, _ = await svc.set_reply(2, user(ADMIN), "2", moderator=False)
    assert not ok  # itself
    ok, _ = await svc.set_reply(2, user(ADMIN), "1", moderator=False)
    assert ok and (await db.get_post(2))["reply_to_post"] == 1
    ok, _ = await svc.set_reply(2, user(APPROVER), "off", moderator=True)
    assert ok and (await db.get_post(2))["reply_to_post"] is None


# ---- removing captions ---------------------------------------------------------------------------------


async def test_remove_caption_before_publish(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "secret caption", bot=bot)])
    ok, text = await svc.edit_post(1, user(ADMIN), "", moderator=False)
    assert ok and "without a caption" in text
    await svc.approve(1, user(APPROVER))
    await svc.tick()
    call = next(c for n, c in bot.calls if n == "send_photo")
    assert call.get("caption") is None and call["photo"] == "fid-P1"


async def test_remove_caption_after_publish(svc, bot, db) -> None:
    await approved(svc, bot, photo_msg(ADMIN, "P1", "old caption", bot=bot))
    await svc.tick()
    ok, text = await svc.edit_post(1, user(ADMIN), "", moderator=False)
    assert ok and "removed" in text
    call = next(c for n, c in bot.calls if n == "edit_message_caption")
    assert call["caption"] == "" and (await db.get_post(1))["caption_override"] == ""
    ok, _ = await svc.edit_post(1, user(ADMIN), None, moderator=False)  # back to the original
    assert ok
    assert [c for n, c in bot.calls if n == "edit_message_caption"][-1]["caption"] == "old caption"


async def test_text_post_cannot_be_emptied(svc, bot) -> None:
    await approved(svc, bot, text_msg(ADMIN, "text only", bot))
    ok, _ = await svc.edit_post(1, user(ADMIN), "", moderator=False)
    assert not ok


async def test_own_edit_reply_is_correct(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "c", bot=bot)])
    ok, text = await svc.edit_post(1, user(ADMIN), "my new caption", moderator=False)
    assert ok and "updated" in text  # used to wrongly say "original caption again"


# ---- clearing buttons ---------------------------------------------------------------------------------------


async def test_clear_buttons_from_channel_posts(svc, bot, db, clock) -> None:
    await svc.set_buttons(None)
    await approved(svc, bot, text_msg(ADMIN, "one", bot))
    await svc.tick()
    clock.advance(minutes=20)
    msgs = [photo_msg(ADMIN, u, group="g", bot=bot) for u in ("A", "B")]
    await svc.submit(user(ADMIN), msgs)
    await svc.approve(2, user(APPROVER))
    await svc.tick()
    assert (await db.get_post(2))["status"] == PUBLISHED
    cleared, failed = await svc.clear_channel_buttons(10)
    assert (cleared, failed) == (1, 0)  # albums are skipped
    call = next(c for n, c in bot.calls if n == "edit_message_reply_markup")
    assert call["chat_id"] == CHANNEL and call["reply_markup"] is None
