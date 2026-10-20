"""Telegram as file storage: copying posts between chats, rollback, and rebuilding from saved file_ids.

The private STORAGE channel holds the permanent snapshot of every submission. Publishing copies from
there (copyMessage / copyMessages, so there is never a "forwarded from" header). When the snapshot is
gone, :func:`rebuild` re-sends the post from the file_ids and captions saved in MongoDB.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from telegram import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    MessageEntity,
)
from telegram.error import BadRequest, TelegramError

log = logging.getLogger(__name__)

_MISSING_HINTS = ("message to copy not found", "message not found", "message_id_invalid",
                  "message to forward not found", "messages not found")
_ALBUM_INPUT = {"photo": InputMediaPhoto, "video": InputMediaVideo, "document": InputMediaDocument,
                "audio": InputMediaAudio}
_CAPTIONLESS = {"video_note", "sticker"}


class CopyIncomplete(Exception):
    """Fewer messages arrived than were sent (partial album). Already rolled back; safe to retry."""


class ContentMissing(Exception):
    """The storage snapshot is gone and the post cannot be rebuilt from saved data (permanent)."""


def is_missing_error(exc: BaseException) -> bool:
    return isinstance(exc, BadRequest) and any(hint in str(exc).lower() for hint in _MISSING_HINTS)


async def copy_batch(bot: Any, to_chat: int, from_chat: int, message_ids: Sequence[int],
                     reply_markup: Any = None) -> list[int]:
    """Copy one message or a whole album (album grouping kept). Returns the new message ids.

    May return fewer ids than requested when some source messages are missing (copyMessages skips them).
    ``reply_markup`` (buttons) only applies to single messages - Telegram albums cannot carry buttons.
    """
    ids = sorted(message_ids)
    if len(ids) == 1:
        extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
        res = await bot.copy_message(chat_id=to_chat, from_chat_id=from_chat, message_id=ids[0], **extra)
        return [res.message_id]
    res = await bot.copy_messages(chat_id=to_chat, from_chat_id=from_chat, message_ids=ids)
    return [r.message_id for r in res]


async def copy_exact(bot: Any, to_chat: int, from_chat: int, message_ids: Sequence[int]) -> list[int]:
    """Like :func:`copy_batch` but all-or-nothing: a partial copy is deleted and CopyIncomplete raised."""
    sent = await copy_batch(bot, to_chat, from_chat, message_ids)
    if len(sent) != len(message_ids):
        await rollback(bot, to_chat, sent)
        raise CopyIncomplete(f"copied {len(sent)} of {len(message_ids)} messages")
    return sent


async def rollback(bot: Any, chat_id: int, message_ids: Sequence[int]) -> None:
    if not message_ids:
        return
    try:
        await bot.delete_messages(chat_id=chat_id, message_ids=list(message_ids))
        log.warning("rolled back %d partial message(s) in %s", len(message_ids), chat_id)
    except TelegramError as e:
        log.error("rollback of %s in %s failed: %s", list(message_ids), chat_id, e)


def _ents(raw: list[dict] | None, bot: Any) -> list[MessageEntity] | None:
    if not raw:
        return None
    return [MessageEntity.de_json(e, bot) for e in raw]


def _caption_kwargs(it: dict, bot: Any) -> dict:
    if not it.get("caption"):
        return {}
    return {"caption": it["caption"], "caption_entities": _ents(it.get("caption_entities"), bot)}


async def _send_one(bot: Any, chat_id: int, it: dict, reply_markup: Any = None) -> int:
    kind, fid, extra = it["media_type"], it.get("file_id"), it.get("extra") or {}
    mk = {"reply_markup": reply_markup} if reply_markup is not None else {}
    if kind == "text":
        msg = await bot.send_message(chat_id=chat_id, text=it["text"], entities=_ents(it.get("entities"), bot), **mk)
    elif kind in ("photo", "video", "animation", "document", "audio", "voice", "video_note", "sticker"):
        if not fid:
            raise ContentMissing(f"no file_id saved for {kind}")
        kwargs: dict[str, Any] = {"chat_id": chat_id, kind: fid, **mk}
        if kind not in _CAPTIONLESS:
            kwargs.update(_caption_kwargs(it, bot))
        if kind in ("photo", "video", "animation") and it.get("has_spoiler"):
            kwargs["has_spoiler"] = True
        msg = await getattr(bot, f"send_{kind}")(**kwargs)
    elif kind == "poll":
        kwargs = {
            "chat_id": chat_id, "question": extra["question"], "options": extra["options"],
            "is_anonymous": extra.get("is_anonymous", True), "type": extra.get("type") or "regular",
            "allows_multiple_answers": bool(extra.get("allows_multiple_answers")), **mk,
        }
        if extra.get("type") == "quiz" and extra.get("correct_option_id") is not None:
            kwargs["correct_option_id"] = extra["correct_option_id"]
            if extra.get("explanation"):
                kwargs["explanation"] = extra["explanation"]
        msg = await bot.send_poll(**kwargs)
    elif kind == "location":
        msg = await bot.send_location(chat_id=chat_id, latitude=extra["latitude"], longitude=extra["longitude"], **mk)
    elif kind == "venue":
        msg = await bot.send_venue(chat_id=chat_id, latitude=extra["latitude"], longitude=extra["longitude"],
                                   title=extra["title"], address=extra["address"], **mk)
    elif kind == "contact":
        msg = await bot.send_contact(chat_id=chat_id, phone_number=extra["phone_number"],
                                     first_name=extra["first_name"], last_name=extra.get("last_name"), **mk)
    elif kind == "dice":
        msg = await bot.send_dice(chat_id=chat_id, emoji=extra.get("emoji"), **mk)
    else:
        raise ContentMissing(f"cannot rebuild content of type {kind!r}")
    return msg.message_id


async def rebuild(bot: Any, chat_id: int, items: Sequence[dict], reply_markup: Any = None) -> list[int]:
    """Re-send a post from saved data. Albums go out as one media group (albums cannot carry buttons)."""
    if not items:
        raise ContentMissing("post has no saved items")
    if len(items) == 1:
        return [await _send_one(bot, chat_id, items[0], reply_markup)]
    media = []
    for it in items:
        cls = _ALBUM_INPUT.get(it["media_type"])
        if cls is None or not it.get("file_id"):
            raise ContentMissing(f"cannot rebuild album item of type {it['media_type']!r}")
        kwargs: dict[str, Any] = {"media": it["file_id"], **_caption_kwargs(it, bot)}
        if it["media_type"] in ("photo", "video") and it.get("has_spoiler"):
            kwargs["has_spoiler"] = True
        media.append(cls(**kwargs))
    sent = await bot.send_media_group(chat_id=chat_id, media=media)
    ids = [m.message_id for m in sent]
    if len(ids) != len(items):
        await rollback(bot, chat_id, ids)
        raise CopyIncomplete(f"rebuilt album sent {len(ids)} of {len(items)} items")
    return ids
