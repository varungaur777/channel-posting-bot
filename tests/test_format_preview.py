"""Caption auto-format, DM preview with edit buttons, /preview and /leaderboard."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.access import Access
from bot.db import CANCELLED
from bot.handlers import dm
from bot.utils import apply_caption, extract_items, style_entities
from tests.conftest import ADMIN, APPROVER, CHANNEL, STRANGER, photo_msg, text_msg, user


def piece(text: str, ent: dict) -> str:
    raw = text.encode("utf-16-le")
    return raw[2 * ent["offset"]: 2 * (ent["offset"] + ent["length"])].decode("utf-16-le")


# ---- style_entities --------------------------------------------------------------------------------


def test_bold_headline_and_quote_with_emoji_and_hindi() -> None:
    text = "🔥 Breaking: big news\n\nपहली लाइन\nsecond line 😀\n"
    ents = style_entities(text, None, "both")
    assert [(e["type"], piece(text, e)) for e in ents] == [
        ("bold", "🔥 Breaking: big news"), ("blockquote", "पहली लाइन\nsecond line 😀")]


def test_style_variants_and_no_change_cases() -> None:
    text = "Title\nbody"
    assert [e["type"] for e in style_entities(text, None, "bold")] == ["bold"]
    assert [e["type"] for e in style_entities(text, None, "quote")] == ["blockquote"]
    assert style_entities(text, None, "off") is None
    assert style_entities("just one line", None, "both") is None
    assert style_entities("Title\n\n   ", None, "both") == [{"type": "bold", "offset": 0, "length": 5}]
    already = [{"type": "bold", "offset": 0, "length": 5}, {"type": "blockquote", "offset": 6, "length": 4}]
    assert style_entities(text, already, "both") is None


def test_existing_formatting_is_kept() -> None:
    text = "Title\nsee https://x.com here"
    link = {"type": "url", "offset": 10, "length": 13}
    ents = style_entities(text, [link], "both")
    assert link in ents and {"type": "bold", "offset": 0, "length": 5} in ents


def test_footer_stays_outside_the_quote() -> None:
    m = photo_msg(ADMIN, "P1", "Headline\nBody text")
    out = apply_caption(extract_items([m]), None, "Join @x", "both")
    cap, ents = out[0]["caption"], out[0]["caption_entities"]
    assert cap == "Headline\nBody text\n\nJoin @x"
    assert [piece(cap, e) for e in ents] == ["Headline", "Body text"]


# ---- publishing with the default style -----------------------------------------------------------------


async def test_multiline_post_published_with_format(svc, bot) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "Big news\nDetails here", bot)])
    await svc.approve(1, user(APPROVER))
    await svc.tick()
    call = next(c for n, c in bot.calls if n == "send_message" and c["chat_id"] == CHANNEL)
    assert call["text"] == "Big news\nDetails here"
    assert [(e.type, e.offset, e.length) for e in call["entities"]] == [("bold", 0, 8), ("blockquote", 9, 12)]


async def test_format_off_copies_unchanged(svc, bot) -> None:
    await svc.set_format_style("off")
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "Big news\nDetails here", bot)])
    await svc.approve(1, user(APPROVER))
    await svc.tick()
    assert any(n == "copy_message" and c["chat_id"] == CHANNEL for n, c in bot.calls)
    assert not any(n == "send_message" and c["chat_id"] == CHANNEL for n, c in bot.calls)
    assert "Auto-format: off" in await svc.stats_text()


# ---- DM preview + edit buttons ---------------------------------------------------------------------------


async def test_submit_sends_dm_preview_with_edit_buttons(svc, bot) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "Hello\nworld", bot)])
    preview = next(c for n, c in bot.calls if n == "send_message" and c["chat_id"] == ADMIN and "entities" in c)
    assert preview["text"] == "Hello\nworld"  # formatted exactly like the channel post
    last = bot.chats[ADMIN][max(bot.chats[ADMIN])]
    assert "submitted!" in last["text"] and "how it will look" in last["text"]
    assert [b.callback_data for row in last["reply_markup"].inline_keyboard for b in row] == \
        ["dme:1", "dmr:1", "dmo:1", "dmx:1"]


class FakeApp:
    def __init__(self) -> None:
        self.handlers = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


def dm_click(data: str, uid: int = ADMIN):
    q = SimpleNamespace(data=data, from_user=user(uid), answer=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=uid, type="private")))
    return SimpleNamespace(callback_query=q), q


async def test_dm_edit_caption_by_reply(cfg, svc, bot, db) -> None:
    acc = Access(cfg, db)
    await acc.load()
    app = FakeApp()
    editor = dm.register(app, cfg, svc, acc)
    on_button = app.handlers[0].callback
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "typo captoin", bot=bot)])

    upd, _ = dm_click("dme:1")
    await on_button(upd, None)
    prompt_id = max(bot.chats[ADMIN])
    assert "Reply to this message with the new caption" in bot.chats[ADMIN][prompt_id]["text"]

    answer = SimpleNamespace(chat_id=ADMIN, text="fixed caption", reply_to_message=SimpleNamespace(message_id=prompt_id))
    assert await editor.try_handle_reply(answer, user(ADMIN)) is True  # not treated as a new post
    assert (await db.get_post(1))["caption_override"] == "fixed caption"
    assert any("Updated preview" in t for t in bot.texts_to(ADMIN))
    assert await editor.try_handle_reply(answer, user(ADMIN)) is False  # prompt used once
    assert await svc.db.get_post(2) is None


async def test_dm_remove_original_cancel_and_permissions(cfg, svc, bot, db) -> None:
    acc = Access(cfg, db)
    await acc.load()
    app = FakeApp()
    dm.register(app, cfg, svc, acc)
    on_button = app.handlers[0].callback
    await svc.submit(user(ADMIN), [photo_msg(ADMIN, "P1", "cap", bot=bot)])
    await on_button(dm_click("dmr:1")[0], None)
    assert (await db.get_post(1))["caption_override"] == ""
    await on_button(dm_click("dmo:1")[0], None)
    assert (await db.get_post(1))["caption_override"] is None
    await on_button(dm_click("dmr:1", uid=STRANGER)[0], None)  # not their post
    assert (await db.get_post(1))["caption_override"] is None
    upd, _ = dm_click("dmx:1")
    await on_button(upd, None)
    assert (await db.get_post(1))["status"] == CANCELLED


# ---- /preview + /leaderboard -----------------------------------------------------------------------------


async def test_preview_command_sends_to_chat(svc, bot) -> None:
    await svc.submit(user(ADMIN), [text_msg(ADMIN, "one line", bot)])
    before = len(bot.chats[APPROVER])
    ok, _ = await svc.send_channel_preview(1, APPROVER)
    assert ok and len(bot.chats[APPROVER]) == before + 1
    ok, _ = await svc.send_channel_preview(99, APPROVER)
    assert not ok


async def test_leaderboard(svc, bot, clock) -> None:
    for t in ("a", "b", "c"):
        await svc.submit(user(ADMIN), [text_msg(ADMIN, t, bot)])
    await svc.submit(user(APPROVER), [text_msg(APPROVER, "z", bot)])
    await svc.approve(1, user(APPROVER))
    await svc.approve(2, user(APPROVER))
    await svc.reject(3, user(APPROVER))
    await svc.tick()
    text = await svc.leaderboard_text()
    assert text.index("User2") < text.index("User3")  # ADMIN (id 2) first
    assert "3 sent · 📢 1 published (33%) · ⏳ 1 waiting · ❌ 1 rejected" in text
    clock.advance(days=40)
    assert "No posts" in await svc.leaderboard_text(30)
