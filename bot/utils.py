"""Small helpers: time, content extraction from Telegram messages, duplicate hash, logging."""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from html import escape
from pathlib import Path
from typing import Any

from telegram import Message

# message fields that differ between two copies of the same content (ignored for "other" content)
_VOLATILE_FIELDS = {
    "message_id", "date", "chat", "from", "sender_chat", "edit_date", "forward_origin", "forward_from",
    "forward_from_chat", "forward_date", "forward_signature", "forward_sender_name", "is_automatic_forward",
    "reply_to_message", "message_thread_id", "media_group_id", "author_signature", "via_bot",
    "has_protected_content", "is_from_offline", "sender_boost_count", "business_connection_id",
    "external_reply", "quote", "reply_markup", "link_preview_options", "sender_business_bot", "effect_id",
}


# ---- time ------------------------------------------------------------------------------------
def utcnow() -> datetime:
    """Aware UTC now, truncated to milliseconds (what MongoDB stores)."""
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def fmt_dt(dt: datetime | None, tz: tzinfo, with_date: bool = True) -> str:
    if dt is None:
        return "-"
    local = as_utc(dt).astimezone(tz)
    return local.strftime("%d %b %Y, %H:%M" if with_date else "%H:%M")


def fmt_delta(delta: timedelta) -> str:
    secs = max(0, int(delta.total_seconds()))
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m} min"
    return f"{secs}s"


def h(text: Any) -> str:
    """HTML-escape for parse_mode=HTML."""
    return escape(str(text if text is not None else ""), quote=False)


def truncate(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


# ---- content extraction ----------------------------------------------------------------------
def _entities(ents: Iterable[Any] | None) -> list[dict] | None:
    out = [e.to_dict() for e in (ents or ())]
    return out or None


def _file(obj: Any) -> dict:
    return {
        "file_id": obj.file_id,
        "file_unique_id": obj.file_unique_id,
        "file_size": getattr(obj, "file_size", None),
        "mime_type": getattr(obj, "mime_type", None),
        "file_name": getattr(obj, "file_name", None),
    }


def extract_item(m: Message) -> dict:
    """Everything needed to rebuild one message without the original (file_ids, captions, entities...)."""
    item: dict[str, Any] = {
        "message_id": m.message_id,
        "media_type": "other",
        "file_id": None,
        "file_unique_id": None,
        "file_size": None,
        "mime_type": None,
        "file_name": None,
        "text": None,
        "entities": None,
        "caption": m.caption,
        "caption_entities": _entities(m.caption_entities),
        "has_spoiler": bool(getattr(m, "has_media_spoiler", False)),
        "extra": None,
    }
    if m.text:
        item.update(media_type="text", text=m.text, entities=_entities(m.entities))
    elif m.photo:
        item.update(media_type="photo", **_file(m.photo[-1]))
    elif m.animation:  # before document: GIFs also carry a document
        item.update(media_type="animation", **_file(m.animation))
    elif m.video:
        item.update(media_type="video", **_file(m.video))
    elif m.video_note:
        item.update(media_type="video_note", **_file(m.video_note))
    elif m.voice:
        item.update(media_type="voice", **_file(m.voice))
    elif m.audio:
        item.update(media_type="audio", **_file(m.audio))
    elif m.document:
        item.update(media_type="document", **_file(m.document))
    elif m.sticker:
        item.update(media_type="sticker", **_file(m.sticker))
    elif m.poll:
        p = m.poll
        item.update(
            media_type="poll",
            extra={
                "question": p.question,
                "options": [o.text for o in p.options],
                "is_anonymous": p.is_anonymous,
                "type": p.type,
                "allows_multiple_answers": p.allows_multiple_answers,
                "correct_option_id": p.correct_option_id,
                "explanation": p.explanation,
            },
        )
    elif m.venue:  # before location: venues also carry a location
        v = m.venue
        item.update(
            media_type="venue",
            extra={"latitude": v.location.latitude, "longitude": v.location.longitude, "title": v.title,
                   "address": v.address},
        )
    elif m.location:
        item.update(media_type="location",
                    extra={"latitude": m.location.latitude, "longitude": m.location.longitude})
    elif m.contact:
        c = m.contact
        item.update(media_type="contact",
                    extra={"phone_number": c.phone_number, "first_name": c.first_name, "last_name": c.last_name})
    elif m.dice:
        item.update(media_type="dice", extra={"emoji": m.dice.emoji})
    else:
        raw = {k: v for k, v in m.to_dict().items() if k not in _VOLATILE_FIELDS}
        item.update(extra={"raw": raw})
    return item


def extract_items(messages: Sequence[Message]) -> list[dict]:
    return [extract_item(m) for m in sorted(messages, key=lambda x: x.message_id)]


def content_type(items: Sequence[dict]) -> str:
    if len(items) == 1:
        return items[0]["media_type"]
    kinds = sorted({i["media_type"] for i in items})
    return f"album ({len(items)}: {'+'.join(kinds)})"


def content_hash(items: Sequence[dict]) -> str:
    """SHA-256 over file_unique_ids + text/caption (non-file content: its data)."""
    parts: list[list[Any]] = []
    for it in items:
        caption = (it.get("caption") or "").strip()
        if it.get("file_unique_id"):
            parts.append(["file", it["file_unique_id"], caption])
        elif it["media_type"] == "text":
            parts.append(["text", (it.get("text") or "").strip()])
        else:
            parts.append(["data", it["media_type"], it.get("extra"), caption])
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def preview_text(items: Sequence[dict]) -> str:
    for it in items:
        if it.get("text"):
            return it["text"]
        if it.get("caption"):
            return it["caption"]
        extra = it.get("extra") or {}
        if extra.get("question"):
            return extra["question"]
        if extra.get("title"):
            return extra["title"]
    return ""


# ---- logging ---------------------------------------------------------------------------------
class RedactFilter(logging.Filter):
    """Belt and braces: never let the bot token reach a log line."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secret:
            msg = record.getMessage()
            if self.secret in msg:
                record.msg = msg.replace(self.secret, "<BOT_TOKEN>")
                record.args = None
        return True


def setup_logging(level: str, *, token: str = "", log_dir: Path | None = None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    redact = RedactFilter(token)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.addFilter(redact)
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "bot.log", maxBytes=2_000_000, backupCount=5,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(redact)
        root.addHandler(fh)
    # httpx logs every request URL, and the URL contains the token
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
