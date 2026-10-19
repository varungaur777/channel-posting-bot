from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from bot.config import ConfigError, load_config, parse_ids
from bot.jsonstore import JsonStore
from bot.utils import RedactFilter, content_hash, content_type, extract_items
from tests.conftest import photo_msg, text_msg

# ---- duplicate hash ----------------------------------------------------------------------------


def test_same_media_same_caption_same_hash() -> None:
    a = extract_items([photo_msg(5, "U1", "hello")])
    b = extract_items([photo_msg(6, "U1", "hello")])  # other user, other message id, same file
    assert content_hash(a) == content_hash(b)


def test_caption_or_file_change_changes_hash() -> None:
    base = content_hash(extract_items([photo_msg(5, "U1", "hello")]))
    assert content_hash(extract_items([photo_msg(5, "U1", "bye")])) != base
    assert content_hash(extract_items([photo_msg(5, "U2", "hello")])) != base


def test_text_hash_ignores_surrounding_whitespace() -> None:
    assert content_hash(extract_items([text_msg(5, "hi there")])) == \
        content_hash(extract_items([text_msg(5, "  hi there \n")]))


def test_album_items_sorted_and_typed() -> None:
    m1, m2 = photo_msg(5, "A", "cap", group="g"), photo_msg(5, "B", group="g")
    items = extract_items([m2, m1])
    assert [i["file_unique_id"] for i in items] == ["A", "B"]
    assert content_type(items) == "album (2: photo)"
    assert items[0]["file_id"] == "fid-A" and items[0]["file_size"] == 123
    assert content_hash(items) != content_hash(items[::-1])


def test_poll_hash_uses_poll_data() -> None:
    from telegram import Chat, Message, Poll, PollOption, User

    def poll_msg(q: str) -> Message:
        poll = Poll(id="p", question=q, options=[PollOption("a", 0), PollOption("b", 0)], total_voter_count=0,
                    is_closed=False, is_anonymous=True, type="regular", allows_multiple_answers=False)
        return Message(message_id=1, date=datetime.now(UTC), chat=Chat(5, "private"),
                       from_user=User(5, "x", False), poll=poll)

    items = extract_items([poll_msg("Q?")])
    assert items[0]["media_type"] == "poll" and items[0]["extra"]["options"] == ["a", "b"]
    assert content_hash(items) == content_hash(extract_items([poll_msg("Q?")]))
    assert content_hash(items) != content_hash(extract_items([poll_msg("Other?")]))


# ---- config ------------------------------------------------------------------------------------

ENV = {"BOT_TOKEN": "1:x", "OWNER_ID": "1", "APPROVAL_GROUP_ID": "-1", "STORAGE_CHANNEL_ID": "-2",
       "CHANNEL_ID": "-3", "RUNTIME_PROFILE": "persistent"}


def test_config_defaults_and_roles() -> None:
    cfg = load_config({**ENV, "SUBMITTER_IDS": "5, 6", "APPROVER_IDS": "7", "MONGO_URI": "mongodb://x"})
    assert cfg.mongo_url == "mongodb://x" and cfg.mongo_db_name == "channel_poster_bot"
    assert (cfg.min_interval_min, cfg.max_interval_min, cfg.dup_window_days) == (10, 15, 30)
    assert cfg.can_submit(1) and cfg.can_approve(1)  # owner is both
    assert cfg.can_submit(5) and not cfg.can_approve(5)
    assert cfg.can_approve(7) and not cfg.can_submit(7)
    assert not cfg.is_staff(99)
    assert cfg.max_runtime_minutes == 0


def test_config_github_actions_profile_defaults_runtime() -> None:
    env = {**ENV, "RUNTIME_PROFILE": "", "GITHUB_ACTIONS": "true"}
    cfg = load_config(env)
    assert cfg.runtime_profile == "github_actions" and cfg.max_runtime_minutes == 340 and not cfg.file_logging
    assert load_config({**env, "MAX_RUNTIME_MINUTES": "5"}).max_runtime_minutes == 5


@pytest.mark.parametrize("bad", [{"BOT_TOKEN": ""}, {"OWNER_ID": "abc"}, {"MIN_INTERVAL_MIN": "20"},
                                 {"TZ_NAME": "Mars/Base"}, {"RUNTIME_PROFILE": "cloud"}])
def test_config_errors(bad: dict) -> None:
    with pytest.raises(ConfigError):
        load_config({**ENV, **bad})


def test_parse_ids() -> None:
    assert parse_ids("1,2 3") == {1, 2, 3}
    with pytest.raises(ConfigError):
        parse_ids("1,abc")


def test_token_is_redacted() -> None:
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "GET https://api.telegram.org/bot%s/getMe", ("123:SECRET",),
                            None)
    RedactFilter("123:SECRET").filter(rec)
    assert "SECRET" not in rec.getMessage()


# ---- JSON fallback store -----------------------------------------------------------------------


async def test_json_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "db.json"
    store = JsonStore(path)
    coll = store["posts"]
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    await coll.insert_one({"_id": 1, "status": "approved", "decided_at": t0 + timedelta(minutes=5), "ids": [7, 8]})
    await coll.insert_one({"_id": 2, "status": "approved", "decided_at": t0, "ids": []})
    with pytest.raises(DuplicateKeyError):
        await coll.insert_one({"_id": 1})
    docs = await coll.find({"status": {"$in": ["approved"]}}).sort([("decided_at", 1), ("_id", 1)]).to_list(None)
    assert [d["_id"] for d in docs] == [2, 1]
    assert (await coll.find_one({"ids": 8}))["_id"] == 1  # array contains
    assert await coll.find_one({"_id": 1, "status": "pending"}) is None
    got = await coll.find_one_and_update({"_id": 1, "status": "approved"}, {"$set": {"status": "x"}, "$inc": {"n": 1},
                                                                             "$push": {"h": 1}},
                                         return_document=ReturnDocument.AFTER)
    assert got["status"] == "x" and got["n"] == 1 and got["h"] == [1]
    assert await coll.find_one({"missing": None, "decided_at": {"$lt": t0 + timedelta(minutes=1)}}) is not None
    # reload from disk keeps datetimes
    again = JsonStore(path)["posts"]
    assert (await again.find_one({"_id": 1}))["decided_at"] == t0 + timedelta(minutes=5)
    up = await again.find_one_and_update({"_id": "c"}, {"$inc": {"seq": 1}}, upsert=True,
                                         return_document=ReturnDocument.AFTER)
    assert up == {"_id": "c", "seq": 1}
