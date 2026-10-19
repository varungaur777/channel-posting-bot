from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from bot.db import APPROVED, FAILED, NEEDS_REVIEW, PUBLISHED
from bot.scheduler import Scheduler
from tests.conftest import ADMIN, APPROVER, CHANNEL, OWNER, STORAGE, photo_msg, text_msg, user


async def approved_post(svc, bot, text: str) -> int:
    pid = await svc.submit(user(ADMIN), [text_msg(ADMIN, text, bot)])
    ok, _ = await svc.approve(pid, user(APPROVER))
    assert ok
    return pid


async def test_first_post_immediate_then_gap(svc, bot, db, clock) -> None:
    await approved_post(svc, bot, "one")
    await approved_post(svc, bot, "two")
    assert await svc.tick() == 1
    state = await db.get_state()
    gap = state["next_publish_at"] - clock.now
    assert timedelta(minutes=10) <= gap <= timedelta(minutes=15)
    assert await svc.tick() is None  # too early
    clock.now = state["next_publish_at"] - timedelta(seconds=1)
    assert await svc.tick() is None
    clock.now = state["next_publish_at"]
    assert await svc.tick() == 2
    assert bot.sent_to(CHANNEL) == [{"text": "one"}, {"text": "two"}]
    post = await db.get_post(1)
    assert post["status"] == PUBLISHED and post["channel_message_ids"]
    assert any("has been published" in t for t in bot.texts_to(ADMIN))


async def test_gap_never_below_min(svc, bot, db, clock) -> None:
    for i in range(20):
        await approved_post(svc, bot, f"p{i}")
    for _ in range(20):
        assert await svc.tick() is not None
        state = await db.get_state()
        assert state["next_publish_at"] - clock.now >= timedelta(minutes=10)
        clock.now = state["next_publish_at"]


async def test_queue_order_is_approval_time_then_id(svc, bot, db, clock) -> None:
    for t in ("a", "b", "c"):
        await svc.submit(user(ADMIN), [text_msg(ADMIN, t, bot)])
    await svc.approve(3, user(APPROVER))
    clock.advance(seconds=5)
    await svc.approve(1, user(APPROVER))
    await svc.approve(2, user(APPROVER))  # same decided_at as #1 -> id decides
    assert [p["_id"] for p in await db.queue()] == [3, 1, 2]
    sched = await svc.schedule()
    assert sched[1][1][0] - sched[0][1][0] == timedelta(minutes=10)  # earliest ETA uses MIN gap
    assert sched[1][1][1] - sched[0][1][1] == timedelta(minutes=15)  # latest ETA uses MAX gap
    order = []
    for _ in range(3):
        order.append(await svc.tick())
        clock.now = (await db.get_state())["next_publish_at"]
    assert order == [3, 1, 2]


async def test_setinterval_applies_after_next_publish(svc, bot, db, clock) -> None:
    await approved_post(svc, bot, "a")
    await approved_post(svc, bot, "b")
    await svc.tick()
    slot = (await db.get_state())["next_publish_at"]
    await svc.set_interval(60, 60)
    assert (await db.get_state())["next_publish_at"] == slot  # current slot unchanged
    clock.now = slot
    await svc.tick()
    assert (await db.get_state())["next_publish_at"] == clock.now + timedelta(minutes=60)


async def test_pause_resume(svc, bot, db) -> None:
    await approved_post(svc, bot, "a")
    await svc.set_paused(True)
    assert await svc.tick() is None
    await svc.set_paused(False)
    assert await svc.tick() == 1


async def test_rejected_and_cancelled_never_published(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "r", bot)])
    await svc.reject(1, user(APPROVER))
    await approved_post(svc, bot, "c")
    await svc.cancel(2, user(OWNER))
    assert await svc.tick() is None
    assert bot.sent_to(CHANNEL) == []


async def test_network_errors_back_off_then_fail(svc, bot, db, clock) -> None:
    await approved_post(svc, bot, "x")
    for n, minutes in enumerate([1, 2, 4, 8, 16], start=1):
        bot.fail["copy_message"].append(NetworkError("connection reset"))
        assert await svc.tick() is None
        post = await db.get_post(1)
        assert post["status"] == APPROVED and post["retries"] == n
        assert post["next_attempt_at"] == clock.now + timedelta(minutes=minutes)
        clock.now = post["next_attempt_at"]
    bot.fail["copy_message"].append(NetworkError("connection reset"))
    await svc.tick()
    post = await db.get_post(1)
    assert post["status"] == FAILED and "retries" in post["last_error"]
    assert any("FAILED" in t for t in bot.texts_to(OWNER))


async def test_backoff_caps_at_30_minutes(svc, bot, db, clock) -> None:
    svc.cfg = dataclasses.replace(svc.cfg, max_retries=10)
    await approved_post(svc, bot, "x")
    delays = []
    for _ in range(7):
        bot.fail["copy_message"].append(NetworkError("down"))
        await svc.tick()
        nat = (await db.get_post(1))["next_attempt_at"]
        delays.append((nat - clock.now) // timedelta(minutes=1))
        clock.now = nat
    assert delays == [1, 2, 4, 8, 16, 30, 30]


async def test_retry_after_is_not_counted(svc, bot, db, clock) -> None:
    await approved_post(svc, bot, "x")
    bot.fail["copy_message"].append(RetryAfter(42))
    await svc.tick()
    post = await db.get_post(1)
    assert post["status"] == APPROVED and post["retries"] == 0
    assert post["next_attempt_at"] == clock.now + timedelta(seconds=43)
    assert await svc.tick() is None  # still waiting
    clock.advance(seconds=43)
    assert await svc.tick() == 1


async def test_timeout_needs_review_no_auto_retry(svc, bot, db, clock) -> None:
    await approved_post(svc, bot, "x")
    bot.fail["copy_message"].append(TimedOut())
    await svc.tick()
    assert (await db.get_post(1))["status"] == NEEDS_REVIEW
    assert any("needs review" in t for t in bot.texts_to(OWNER))
    clock.advance(hours=1)
    assert await svc.tick() is None
    ok, _ = await svc.retry(1, user(OWNER))
    assert ok and await svc.tick() == 1


async def test_forbidden_fails_immediately(svc, bot, db) -> None:
    await approved_post(svc, bot, "x")
    bot.fail["copy_message"].append(Forbidden("bot is not an admin"))
    await svc.tick()
    post = await db.get_post(1)
    assert post["status"] == FAILED and post["retries"] == 0


async def test_post_in_backoff_does_not_block_queue(svc, bot, db, clock) -> None:
    await approved_post(svc, bot, "first")
    await approved_post(svc, bot, "second")
    bot.fail["copy_message"].append(NetworkError("blip"))
    await svc.tick()  # #1 goes into backoff
    clock.advance(seconds=20)
    assert await svc.tick() == 2
    clock.now = max((await db.get_state())["next_publish_at"], (await db.get_post(1))["next_attempt_at"])
    assert await svc.tick() == 1


async def test_missing_storage_message_is_rebuilt(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "nice pic", bot=bot)])
    await svc.approve(1, user(APPROVER))
    bot.chats[STORAGE].clear()  # someone deleted the snapshot
    assert await svc.tick() == 1
    call = next(c for n, c in bot.calls if n == "send_photo")
    assert call["photo"] == "fid-P1" and call["caption"] == "nice pic" and call["chat_id"] == CHANNEL


async def test_partial_album_rolls_back_and_rebuilds(svc, bot, db) -> None:
    msgs = [photo_msg(ADMIN, u, group="g", bot=bot) for u in ("A", "B")]
    await svc.submit(user(ADMIN), msgs)
    await svc.approve(1, user(APPROVER))
    post = await db.get_post(1)
    bot.chats[STORAGE].pop(post["storage_message_ids"][1])  # half the album is gone
    assert await svc.tick() == 1
    assert any(n == "delete_messages" and c["chat_id"] == CHANNEL for n, c in bot.calls)
    assert [m["media"] for m in bot.sent_to(CHANNEL)] == ["fid-A", "fid-B"]


async def test_rebuild_failure_marks_failed(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", bot=bot)])
    await svc.approve(1, user(APPROVER))
    bot.chats[STORAGE].clear()
    bot.fail["send_photo"].append(BadRequest("Wrong file identifier"))
    await svc.tick()
    assert (await db.get_post(1))["status"] == FAILED
    assert any("FAILED" in t for t in bot.texts_to(OWNER))


async def test_stop_mid_publish_marks_needs_review(svc, bot, db) -> None:
    await approved_post(svc, bot, "slow")
    bot.hang.add("copy_message")
    sched = Scheduler(svc, tick_seconds=0.01)
    sched.start()
    await asyncio.sleep(0.05)
    await sched.stop(grace_s=0.05)
    assert (await db.get_post(1))["status"] == NEEDS_REVIEW


@pytest.mark.parametrize("status_before", ["publishing"])
async def test_tick_ignores_claimed_posts(svc, bot, db, status_before) -> None:
    await approved_post(svc, bot, "x")
    await db.transition(1, [APPROVED], status_before)
    assert await svc.tick() is None


# ---- post now ------------------------------------------------------------------------------------


async def test_post_now_skips_gap_from_pending(svc, bot, db, clock) -> None:
    await approved_post(svc, bot, "regular")
    assert await svc.tick() == 1  # regular post -> next slot 10-15 min away
    slot = (await db.get_state())["next_publish_at"]
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "breaking news", bot)])  # still pending
    clock.advance(minutes=1)
    ok, text = await svc.publish_now(2, user(APPROVER))
    assert ok and "published now" in text
    post = await db.get_post(2)
    assert post["status"] == "published" and post["posted_now"] and post["decided_by"] == APPROVER
    assert bot.sent_to(CHANNEL)[-1] == {"text": "breaking news"}
    new_slot = (await db.get_state())["next_publish_at"]
    assert new_slot >= slot  # queue never moves earlier
    assert new_slot - clock.now >= timedelta(minutes=10)  # and waits a full gap after the urgent post


async def test_post_now_scheduled_post_ignores_pause_and_backoff(svc, bot, db) -> None:
    await approved_post(svc, bot, "x")
    bot.fail["copy_message"].append(NetworkError("blip"))
    await svc.tick()  # now in backoff
    await svc.set_paused(True)
    ok, _ = await svc.publish_now(1, user(OWNER))
    assert ok and (await db.get_post(1))["status"] == "published"


async def test_post_now_refuses_rejected_and_reports_failure(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "no", bot)])
    await svc.reject(1, user(APPROVER))
    ok, text = await svc.publish_now(1, user(OWNER))
    assert not ok and "Rejected" in text
    assert bot.sent_to(CHANNEL) == []
    await approved_post(svc, bot, "y")
    bot.fail["copy_message"].append(Forbidden("bot is not an admin"))
    ok, text = await svc.publish_now(2, user(OWNER))
    assert not ok and "Failed" in text
