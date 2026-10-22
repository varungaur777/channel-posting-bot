from __future__ import annotations

import asyncio

from bot.db import APPROVED, CANCELLED, FAILED, PENDING, REJECTED
from tests.conftest import ADMIN, APPROVAL, APPROVER, OWNER, STORAGE, STRANGER, photo_msg, text_msg, user


async def test_submit_text_goes_storage_then_approval(svc, bot, db) -> None:
    msg = text_msg(ADMIN, "Hello channel", bot)
    pid = await svc.submit(user(ADMIN), [msg])
    assert pid == 1
    post = await db.get_post(1)
    assert post["status"] == PENDING and post["content_type"] == "text"
    assert post["storage_message_ids"] and post["approval_message_ids"] and post["control_message_id"]
    # storage copy is made from the DM, approval copy is made from storage (never from the DM)
    copies = [c for n, c in bot.calls if n.startswith("copy_")]
    assert copies[0]["chat_id"] == STORAGE and copies[0]["from_chat_id"] == ADMIN
    assert copies[1]["chat_id"] == APPROVAL and copies[1]["from_chat_id"] == STORAGE
    control = bot.chats[APPROVAL][post["control_message_id"]]
    assert "Post #1" in control["text"] and control["reply_markup"] is not None
    assert any("submitted!" in t for t in bot.texts_to(ADMIN))


async def test_album_is_one_post(svc, bot, db) -> None:
    msgs = [photo_msg(ADMIN, u, "cap" if u == "A" else None, group="g1", bot=bot) for u in ("A", "B", "C")]
    await svc.submit(user(ADMIN), msgs)
    post = await db.get_post(1)
    assert len(post["items"]) == 3 and len(post["storage_message_ids"]) == 3
    assert post["items"][0]["caption"] == "cap" and post["items"][1]["file_unique_id"] == "B"
    copies = [(n, c["chat_id"]) for n, c in bot.calls if n.startswith("copy_")]
    assert copies == [("copy_messages", STORAGE), ("copy_messages", APPROVAL), ("copy_messages", ADMIN)]  # +DM preview


async def test_duplicate_blocked_until_rejected(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "X", "same", bot=bot)])
    assert await svc.submit(user(ADMIN), [photo_msg(ADMIN, "X", "same", bot=bot)]) is None
    assert any("Post #1" in t and "Duplicate" in t for t in bot.texts_to(ADMIN))
    await svc.reject(1, user(APPROVER))
    assert await svc.submit(user(ADMIN), [photo_msg(ADMIN, "X", "same", bot=bot)]) == 2


async def test_duplicate_window(svc, bot, clock) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "old news", bot)])
    clock.advance(days=31)
    assert await svc.submit(user(ADMIN), [text_msg(ADMIN, "old news", bot)]) == 2


async def test_storage_failure_marks_failed(svc, bot, db) -> None:
    from telegram.error import Forbidden

    bot.fail["copy_message"].append(Forbidden("bot is not a member of the channel chat"))
    assert await svc.submit(user(ADMIN), [text_msg(ADMIN, "x", bot)]) is None
    assert (await db.get_post(1))["status"] == FAILED
    assert any("FAILED" in t for t in bot.texts_to(OWNER))


async def test_double_approve_counts_once(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "race", bot)])
    results = await asyncio.gather(svc.approve(1, user(APPROVER)), svc.approve(1, user(OWNER)),
                                   svc.reject(1, user(APPROVER)))
    assert sum(ok for ok, _ in results) == 1
    post = await db.get_post(1)
    assert post["status"] in (APPROVED, REJECTED)
    assert len([e for e in post["history"] if e["event"] in ("approved", "rejected")]) == 1


async def test_approve_updates_control_and_notifies(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "hi", bot)])
    ok, text = await svc.approve(1, user(APPROVER))
    assert ok and "ETA" in text
    post = await db.get_post(1)
    control = bot.chats[APPROVAL][post["control_message_id"]]
    assert "Scheduled" in control["text"] and "ETA" in control["text"]
    buttons = [b.callback_data for row in control["reply_markup"].inline_keyboard for b in row]
    assert buttons == ["pn:1", "sc:1", "tp:1", "ec:1", "cx:1"]  # scheduled-post buttons
    assert any("has been approved" in t and "Expected in the channel" in t for t in bot.texts_to(ADMIN))
    ok, text = await svc.approve(1, user(APPROVER))
    assert not ok and "already" in text


async def test_reject_reason_reaches_submitter(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "bad", bot)])
    ok, _ = await svc.reject(1, user(APPROVER), "spelling galat hai")
    assert ok and (await db.get_post(1))["reject_reason"] == "spelling galat hai"
    assert any("spelling galat hai" in t for t in bot.texts_to(ADMIN))


async def test_cancel_permissions(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "a", bot)])
    ok, _ = await svc.cancel(1, user(APPROVER))  # not owner, not the submitter
    assert not ok
    ok, _ = await svc.cancel(1, user(ADMIN))
    assert ok and (await db.get_post(1))["status"] == CANCELLED
    ok, _ = await svc.cancel(1, user(OWNER))  # already cancelled
    assert not ok


async def test_retry_only_owner_and_only_approved_posts(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "a", bot)])
    ok, _ = await svc.retry(1, user(OWNER))
    assert not ok  # pending
    ok, _ = await svc.retry(1, user(STRANGER))
    assert not ok


async def test_edit_after_submit_gets_reply(svc, bot) -> None:
    msg = text_msg(ADMIN, "v1", bot)
    await svc.submit(user(ADMIN), [msg])
    await svc.handle_edit(msg)
    assert any("Edits are not tracked" in t and "/cancel 1" in t for t in bot.texts_to(ADMIN))
