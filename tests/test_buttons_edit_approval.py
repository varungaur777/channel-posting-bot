"""Join button under posts, editing captions after publishing, and turning approval off."""

from __future__ import annotations

from bot.db import APPROVED, PUBLISHED
from tests.conftest import ADMIN, APPROVER, CHANNEL, OWNER, STRANGER, photo_msg, text_msg, user


async def approved(svc, bot, msg) -> int:
    pid = await svc.submit(user(ADMIN), [msg])
    await svc.approve(pid, user(APPROVER))
    return pid


def urls(markup) -> list[str]:
    return [b.url for row in markup.inline_keyboard for b in row] if markup else []


# ---- buttons -------------------------------------------------------------------------------------


async def test_default_join_button_under_posts(svc, bot) -> None:
    await svc.load_channel_info()
    await approved(svc, bot, text_msg(ADMIN, "hello", bot))
    await svc.tick()
    call = next(c for n, c in bot.calls if n == "copy_message" and c["chat_id"] == CHANNEL)
    assert urls(call["reply_markup"]) == ["https://t.me/mychannel"]
    assert call["reply_markup"].inline_keyboard[0][0].text == "📢 Join My Channel"
    storage_copy = next(c for n, c in bot.calls if n == "copy_message" and c["chat_id"] != CHANNEL)
    assert "reply_markup" not in storage_copy  # buttons only on the channel post


async def test_button_settings(svc, bot) -> None:
    await svc.load_channel_info()
    await svc.set_buttons([{"text": "Website", "url": "https://example.com"}, {"text": "Chat", "url": "https://t.me/x"}])
    assert urls(await svc.post_buttons()) == ["https://example.com", "https://t.me/x"]
    await svc.set_buttons([])
    assert await svc.post_buttons() is None
    await svc.set_buttons(None)
    assert urls(await svc.post_buttons()) == ["https://t.me/mychannel"]


async def test_button_sent_with_rebuilt_post_but_not_album(svc, bot, clock) -> None:
    await svc.load_channel_info()
    await svc.set_footer("f")  # forces the rebuild path
    await approved(svc, bot, photo_msg(ADMIN, "P1", "pic", bot=bot))
    msgs = [photo_msg(ADMIN, u, group="g", bot=bot) for u in ("A", "B")]
    await svc.submit(user(ADMIN), msgs)
    await svc.approve(2, user(APPROVER))
    await svc.tick()
    clock.advance(minutes=20)
    await svc.tick()
    photo = next(c for n, c in bot.calls if n == "send_photo")
    assert urls(photo["reply_markup"]) == ["https://t.me/mychannel"]
    album = next(c for n, c in bot.calls if n == "send_media_group")
    assert "reply_markup" not in album


# ---- edit after publish -----------------------------------------------------------------------------


async def test_edit_published_text_post(svc, bot, db) -> None:
    await svc.load_channel_info()
    await approved(svc, bot, text_msg(ADMIN, "old text", bot))
    await svc.tick()
    ok, text = await svc.edit_post(1, user(ADMIN), "new text", moderator=False)  # submitter edits own post
    assert ok, text
    call = next(c for n, c in bot.calls if n == "edit_message_text" and c["chat_id"] == CHANNEL)
    assert call["text"] == "new text" and urls(call["reply_markup"]) == ["https://t.me/mychannel"]
    post = await db.get_post(1)
    assert post["status"] == PUBLISHED and post["caption_override"] == "new text"


async def test_edit_published_photo_caption_with_footer(svc, bot, db) -> None:
    await approved(svc, bot, photo_msg(ADMIN, "P1", "old", bot=bot))
    await svc.tick()
    await svc.set_footer("Join @x")
    ok, _ = await svc.edit_post(1, user(APPROVER), "fresh caption", moderator=True)
    assert ok
    call = next(c for n, c in bot.calls if n == "edit_message_caption")
    assert call["caption"] == "fresh caption\n\nJoin @x"
    assert call["message_id"] == (await db.get_post(1))["channel_message_ids"][0]
    assert any("was edited" in t for t in bot.texts_to(ADMIN))  # submitter told


async def test_edit_permissions_and_states(svc, bot, db) -> None:
    await approved(svc, bot, text_msg(ADMIN, "mine", bot))
    ok, text = await svc.edit_post(1, user(STRANGER), "hack", moderator=False)
    assert not ok and "own posts" in text
    ok, _ = await svc.edit_post(1, user(ADMIN), "changed before publish", moderator=False)  # scheduled: override
    assert ok and (await db.get_post(1))["caption_override"] == "changed before publish"
    assert not any("edited the caption" in t for t in bot.texts_to(ADMIN))  # no self-notification
    await svc.tick()
    assert next(c for n, c in bot.calls if n == "send_message" and c["chat_id"] == CHANNEL)["text"] == \
        "changed before publish"
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "other", bot)])
    await svc.reject(2, user(APPROVER))
    ok, _ = await svc.edit_post(2, user(OWNER), "x", moderator=True)
    assert not ok


# ---- approval on/off ----------------------------------------------------------------------------------


async def test_approval_off_goes_straight_to_queue(svc, bot, db) -> None:
    assert await svc.approval_required()
    await svc.set_approval_required(False)
    pid = await svc.submit(user(ADMIN), [text_msg(ADMIN, "no wait", bot)])
    post = await db.get_post(pid)
    assert post["status"] == APPROVED and post["auto_approved"] and post["control_message_id"]
    assert any("approval is currently off" in t for t in bot.texts_to(ADMIN))
    assert await svc.tick() == pid
    await svc.set_approval_required(True)
    pid2 = await svc.submit(user(ADMIN), [text_msg(ADMIN, "wait again", bot)])
    assert (await db.get_post(pid2))["status"] == "pending"
    assert "OFF" not in await svc.stats_text()
