"""Request changes, caption edits, footer, fixed-time schedule, quiet hours, move to top, reminders, report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from bot.access import Access
from bot.db import APPROVED, CHANGES_REQUESTED, PUBLISHED
from bot.handlers import approval
from bot.schedule_utils import in_quiet, parse_when, skip_quiet
from tests.conftest import ADMIN, APPROVAL, APPROVER, CHANNEL, OWNER, photo_msg, text_msg, user

IST = ZoneInfo("Asia/Kolkata")  # test clock starts 2026-01-01 12:00 UTC = 17:30 IST


async def approved(svc, bot, text: str) -> int:
    pid = await svc.submit(user(ADMIN), [text_msg(ADMIN, text, bot)])
    await svc.approve(pid, user(APPROVER))
    return pid


# ---- request changes ---------------------------------------------------------------------------


async def test_request_changes_notifies_and_allows_resubmit(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "helo wrld", bot)])
    ok, _ = await svc.request_changes(1, user(APPROVER), "Fix the spelling: hello world")
    assert ok
    post = await db.get_post(1)
    assert post["status"] == CHANGES_REQUESTED and post["changes_note"] == "Fix the spelling: hello world"
    assert any("Changes requested" in t and "Fix the spelling" in t for t in bot.texts_to(ADMIN))
    control = bot.chats[APPROVAL][post["control_message_id"]]
    assert "Changes requested" in control["text"] and control.get("reply_markup") is None
    assert await svc.submit(user(ADMIN), [text_msg(ADMIN, "helo wrld", bot)]) == 2  # not a blocked duplicate
    ok, _ = await svc.request_changes(1, user(APPROVER), "again")  # already decided
    assert not ok
    assert await svc.tick() is None  # never published


async def test_request_changes_needs_a_note(svc, bot) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "x", bot)])
    ok, _ = await svc.request_changes(1, user(APPROVER), "  ")
    assert not ok


# ---- caption edits + footer --------------------------------------------------------------------


async def test_edited_caption_is_published(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "old caption", bot=bot)])
    ok, _ = await svc.set_caption(1, user(APPROVER), "new caption")
    assert ok and any("edited the caption" in t for t in bot.texts_to(ADMIN))
    await svc.approve(1, user(APPROVER))
    assert await svc.tick() == 1
    call = next(c for n, c in bot.calls if n == "send_photo")
    assert call["photo"] == "fid-P1" and call["caption"] == "new caption" and call["chat_id"] == CHANNEL


async def test_caption_off_restores_original_copy(svc, bot, db) -> None:
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "orig", bot=bot)])
    await svc.set_caption(1, user(APPROVER), "temp")
    ok, _ = await svc.set_caption(1, user(APPROVER), None)
    assert ok and (await db.get_post(1))["caption_override"] is None
    await svc.approve(1, user(APPROVER))
    await svc.tick()
    assert not any(n == "send_photo" for n, _ in bot.calls)  # plain copy from storage again


async def test_footer_on_text_photo_and_album(svc, bot, clock) -> None:
    await svc.set_footer("Join @mychannel")
    await approved(svc, bot, "hello")
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "pic", bot=bot)])
    await svc.approve(2, user(APPROVER))
    msgs = [photo_msg(ADMIN, u, "album cap" if u == "A" else None, group="g", bot=bot) for u in ("A", "B")]
    await svc.submit(user(ADMIN), msgs)
    await svc.approve(3, user(APPROVER))
    for _ in range(3):
        assert await svc.tick() is not None
        clock.advance(minutes=20)
    sent_text = next(c for n, c in bot.calls if n == "send_message" and c["chat_id"] == CHANNEL)
    assert sent_text["text"] == "hello\n\nJoin @mychannel"
    assert next(c for n, c in bot.calls if n == "send_photo")["caption"] == "pic\n\nJoin @mychannel"
    media = next(c for n, c in bot.calls if n == "send_media_group")["media"]
    assert media[0].caption == "album cap\n\nJoin @mychannel" and media[1].caption is None


async def test_caption_validation(svc, bot) -> None:
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "c", bot=bot)])
    ok, text = await svc.set_caption(1, user(APPROVER), "x" * 1025)
    assert not ok and "Too long" in text
    await svc.reject(1, user(APPROVER))
    ok, _ = await svc.set_caption(1, user(APPROVER), "late")
    assert not ok


# ---- fixed-time schedule -------------------------------------------------------------------------


def test_parse_when() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # 17:30 IST
    assert parse_when("18:30", now, IST) == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    assert parse_when("09:00", now, IST) == datetime(2026, 1, 2, 3, 30, tzinfo=UTC)  # tomorrow
    assert parse_when("05-01 10:00", now, IST) == datetime(2026, 1, 5, 4, 30, tzinfo=UTC)
    assert parse_when("+90", now, IST) == now + timedelta(minutes=90)
    assert parse_when("+2h", now, IST) == now + timedelta(hours=2)
    for bad in ("25:00", "31-02 10:00", "tomorrow", "01-01-2020 10:00", "+0"):
        assert parse_when(bad, now, IST) is None


async def test_fixed_time_post_waits_then_ignores_gap(svc, bot, db, clock) -> None:
    await approved(svc, bot, "regular 1")
    await approved(svc, bot, "regular 2")
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "evening special", bot)])
    ok, _ = await svc.schedule_post(3, user(APPROVER), "+5")  # pending -> approved + fixed time
    assert ok and (await db.get_post(3))["status"] == APPROVED
    assert await svc.tick() == 1  # regular queue unaffected
    assert await svc.tick() is None  # gap running, special not due yet
    clock.advance(minutes=5)
    assert await svc.tick() == 3  # due: goes out despite the gap
    assert (await db.get_post(2))["status"] == APPROVED
    sched = await svc.schedule()
    assert [p["_id"] for p, _ in sched] == [2]


async def test_schedule_off_returns_to_queue(svc, bot, db) -> None:
    await approved(svc, bot, "x")
    await svc.schedule_post(1, user(APPROVER), "+60")
    assert await svc.tick() is None
    ok, _ = await svc.schedule_post(1, user(APPROVER), "off")
    assert ok and await svc.tick() == 1


# ---- quiet hours --------------------------------------------------------------------------------


def test_quiet_window_math() -> None:
    at = lambda h, m=0: datetime(2026, 1, 1, h, m, tzinfo=IST).astimezone(UTC)  # noqa: E731
    assert in_quiet(at(23, 30), (23, 7), IST) and in_quiet(at(3), (23, 7), IST) and not in_quiet(at(7), (23, 7), IST)
    assert in_quiet(at(14), (13, 15), IST) and not in_quiet(at(15), (13, 15), IST)
    assert skip_quiet(at(23, 30), (23, 7), IST) == datetime(2026, 1, 2, 7, 0, tzinfo=IST).astimezone(UTC)
    assert skip_quiet(at(12), (23, 7), IST) == at(12)


async def test_quiet_hours_hold_regular_posts(svc, bot, db, clock) -> None:
    await approved(svc, bot, "x")
    await svc.set_quiet_hours(17, 19)  # it is 17:30 IST now
    assert await svc.tick() is None
    (_, (early, _)), = await svc.schedule()
    assert early.astimezone(IST).hour == 19 and early.astimezone(IST).minute == 0
    ok, _ = await svc.publish_now(1, user(OWNER))  # Post Now still works
    assert ok
    await approved(svc, bot, "y")
    clock.advance(hours=1, minutes=30)  # 19:00 IST
    assert await svc.tick() == 2


# ---- move to top ----------------------------------------------------------------------------------


async def test_move_to_top(svc, bot, db, clock) -> None:
    for t in ("a", "b", "c"):
        await approved(svc, bot, t)
        clock.advance(seconds=1)
    ok, _ = await svc.move_to_top(3, user(APPROVER))
    assert ok and [p["_id"] for p in await db.queue()] == [3, 1, 2]
    assert await svc.tick() == 3


# ---- reminders + daily report --------------------------------------------------------------------


async def test_pending_reminder_sent_once(svc, bot, clock) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "waiting", bot)])
    await svc.housekeeping()
    assert not any("waiting for approval" in t for t in bot.texts_to(APPROVAL))
    clock.advance(minutes=61)
    await svc.housekeeping()
    await svc.housekeeping()
    assert sum("waiting for approval" in t for t in bot.texts_to(APPROVAL)) == 1


async def test_daily_report_once_per_day(svc, bot, db, clock) -> None:
    await approved(svc, bot, "x")
    await svc.tick()
    await svc.housekeeping()  # 17:30 IST < 22:00
    assert not any("Report" in t for t in bot.texts_to(OWNER))
    clock.advance(hours=5)  # 22:30 IST
    await svc.housekeeping()
    await svc.housekeeping()
    reports = [t for t in bot.texts_to(OWNER) if "Report" in t]
    assert len(reports) == 1 and "Published: <b>1</b>" in reports[0]


async def test_stats_shows_new_settings(svc) -> None:
    await svc.set_quiet_hours(23, 7)
    await svc.set_footer("Join @x")
    text = await svc.stats_text()
    assert "23:00 – 07:00" in text and "Join @x" in text and "Changes requested" in text


# ---- reply prompts in the approval group ----------------------------------------------------------


class FakeApp:
    def __init__(self) -> None:
        self.handlers = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


async def test_request_changes_button_and_reply(cfg, svc, bot, db) -> None:
    acc = Access(cfg, db)
    await acc.load()
    app = FakeApp()
    approval.register(app, cfg, svc, acc)
    on_button, on_reply = app.handlers[0].callback, app.handlers[1].callback
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "draft", bot)])

    q = SimpleNamespace(data="rc:1", from_user=user(APPROVER), answer=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=APPROVAL)))
    await on_button(SimpleNamespace(callback_query=q), None)
    prompt_id = max(bot.chats[APPROVAL])
    assert "reply to this message" in bot.chats[APPROVAL][prompt_id]["text"]

    reply_msg = SimpleNamespace(text="Add the date please", reply_to_message=SimpleNamespace(message_id=prompt_id),
                                reply_text=AsyncMock())
    await on_reply(SimpleNamespace(effective_message=reply_msg, effective_user=user(APPROVER)), None)
    post = await db.get_post(1)
    assert post["status"] == CHANGES_REQUESTED and post["changes_note"] == "Add the date please"
    assert any("Add the date please" in t for t in bot.texts_to(ADMIN))

    # replying to the same prompt again does nothing
    await on_reply(SimpleNamespace(effective_message=reply_msg, effective_user=user(APPROVER)), None)
    assert reply_msg.reply_text.await_count == 1
    assert (await db.get_post(1))["status"] != PUBLISHED
