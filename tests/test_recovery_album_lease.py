from __future__ import annotations

import asyncio
from datetime import timedelta

from bot.db import APPROVED, FAILED, NEEDS_REVIEW, PENDING, PUBLISHING
from bot.handlers.submit import AlbumBuffer
from bot.lease import Lease
from tests.conftest import ADMIN, APPROVER, OWNER, Clock, photo_msg, text_msg, user

# ---- restart recovery --------------------------------------------------------------------------


async def test_startup_recovery(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "a", bot)])
    await svc.approve(1, user(APPROVER))
    await db.transition(1, [APPROVED], PUBLISHING)  # crashed mid-publish
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "b", bot)])  # healthy pending
    await db.insert_post({"_id": 3, "status": PENDING, "submitter_id": ADMIN, "control_message_id": None,
                          "history": []})  # never reached the approval group
    await svc.recover_on_startup()
    assert (await db.get_post(1))["status"] == NEEDS_REVIEW
    assert (await db.get_post(2))["status"] == PENDING
    assert (await db.get_post(3))["status"] == FAILED
    owner_msgs = bot.texts_to(OWNER)
    assert sum("#1" in t and "needs_review" in t for t in owner_msgs) == 1
    assert await svc.tick() is None  # needs_review is never auto-published


# ---- album grouping ----------------------------------------------------------------------------


async def test_album_buffer_groups_one_post() -> None:
    flushed: list[list] = []

    async def on_flush(msgs):
        flushed.append(msgs)

    buf = AlbumBuffer(0.05, on_flush)
    a = [photo_msg(ADMIN, u, group="g1") for u in ("A", "B", "C")]
    b = [photo_msg(ADMIN, u, group="g2") for u in ("D", "E")]
    for m in (a[0], b[0], a[2], a[1], b[1]):
        buf.add(m)
        await asyncio.sleep(0.01)  # arrive one by one, each within the window
    assert flushed == []
    await asyncio.sleep(0.12)
    assert sorted(len(f) for f in flushed) == [2, 3]
    album = next(f for f in flushed if len(f) == 3)
    assert [m.message_id for m in album] == sorted(m.message_id for m in a)


async def test_album_buffer_flush_all_on_shutdown() -> None:
    flushed: list[list] = []

    async def on_flush(msgs):
        flushed.append(msgs)

    buf = AlbumBuffer(10, on_flush)
    buf.add(photo_msg(ADMIN, "A", group="g"))
    buf.add(photo_msg(ADMIN, "B", group="g"))
    await buf.flush_all()
    assert len(flushed) == 1 and len(flushed[0]) == 2 and buf.pending == 0


# ---- instance lease ----------------------------------------------------------------------------


async def test_lease_acquire_wait_release(mongo_db) -> None:
    clock = Clock()
    a = Lease(mongo_db.lease, expiry_s=120, poll_s=0.01, clock=clock, holder="A")
    b = Lease(mongo_db.lease, expiry_s=120, poll_s=0.01, clock=clock, holder="B")
    assert await a.try_acquire()
    assert not await b.try_acquire()  # A is live -> B must wait

    waiter = asyncio.create_task(b.acquire())
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await a.release()
    assert await asyncio.wait_for(waiter, 1) is True
    assert (await b.current())["holder"] == "B"
    await b.release()
    assert await b.current() is None


async def test_lease_expiry_takeover_and_lost_heartbeat(mongo_db) -> None:
    clock = Clock()
    a = Lease(mongo_db.lease, expiry_s=120, heartbeat_s=0.01, clock=clock, holder="A")
    b = Lease(mongo_db.lease, expiry_s=120, clock=clock, holder="B")
    assert await a.try_acquire()
    clock.advance(seconds=60)
    assert await a.renew()  # heartbeat pushes expiry forward
    clock.advance(seconds=100)
    assert not await b.try_acquire()  # still live thanks to the renew
    clock.advance(seconds=30)
    assert await b.try_acquire()  # A stopped beating -> expired -> B takes over

    lost: list[str] = []
    a.start_heartbeat(lost.append)
    await asyncio.sleep(0.05)
    assert lost == ["lease lost"] and not a.held
    await a.release()
    assert (await b.current())["holder"] == "B"  # A's release never deletes B's lease


async def test_lease_wait_stops_on_shutdown(mongo_db) -> None:
    a = Lease(mongo_db.lease, holder="A")
    b = Lease(mongo_db.lease, poll_s=0.01, holder="B")
    await a.try_acquire()
    stop = asyncio.Event()
    task = asyncio.create_task(b.acquire(stop))
    await asyncio.sleep(0.03)
    stop.set()
    assert await asyncio.wait_for(task, 1) is False


async def test_lease_reacquire_by_same_holder(mongo_db) -> None:
    clock = Clock()
    a = Lease(mongo_db.lease, clock=clock, holder="A")
    assert await a.try_acquire()
    clock.advance(seconds=5)
    assert await a.try_acquire()
    doc = await a.current()
    assert doc["expires_at"] == clock.now + timedelta(seconds=120)
