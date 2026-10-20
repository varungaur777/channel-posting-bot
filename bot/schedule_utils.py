"""Time helpers: quiet hours and parsing the time given to /schedule (all in the bot's TZ_NAME)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo

Window = tuple[int, int] | None  # (start_hour, end_hour) local time, end exclusive; may wrap midnight


def quiet_window(state: dict) -> Window:
    start, end = state.get("quiet_start"), state.get("quiet_end")
    if start is None or end is None or start == end:
        return None
    return int(start), int(end)


def in_quiet(dt: datetime, window: Window, tz: tzinfo) -> bool:
    if window is None:
        return False
    start, end = window
    hour = dt.astimezone(tz).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def skip_quiet(dt: datetime, window: Window, tz: tzinfo) -> datetime:
    """Move ``dt`` to the end of the quiet window if it falls inside it."""
    if not in_quiet(dt, window, tz):
        return dt
    local = dt.astimezone(tz)
    end = local.replace(hour=window[1], minute=0, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.astimezone(UTC)


_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_DATE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{4}))?$")
_REL = re.compile(r"^\+(\d+)\s*(m|min|h|hr)?$", re.IGNORECASE)


def parse_when(text: str, now: datetime, tz: tzinfo) -> datetime | None:
    """'18:30' (today, or tomorrow if already past) · '25-10 18:30' · '25-10-2026 18:30' · '+90' / '+2h'.

    Returns an aware UTC datetime in the future, or None if the text is not understood / in the past.
    """
    text = " ".join(text.strip().split())
    rel = _REL.match(text)
    if rel:
        n = int(rel.group(1))
        delta = timedelta(hours=n) if (rel.group(2) or "m").lower().startswith("h") else timedelta(minutes=n)
        return now + delta if n > 0 else None
    parts = text.split(" ")
    date_part, time_part = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
    tm = _TIME.match(time_part)
    if not tm or len(parts) > 2:
        return None
    hour, minute = int(tm.group(1)), int(tm.group(2))
    if hour > 23 or minute > 59:
        return None
    local_now = now.astimezone(tz)
    try:
        if date_part is None:
            when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when <= local_now:
                when += timedelta(days=1)
        else:
            dm = _DATE.match(date_part)
            if not dm:
                return None
            year = int(dm.group(3)) if dm.group(3) else local_now.year
            when = local_now.replace(year=year, month=int(dm.group(2)), day=int(dm.group(1)), hour=hour,
                                     minute=minute, second=0, microsecond=0)
            if when <= local_now and not dm.group(3):
                when = when.replace(year=year + 1)
    except ValueError:  # e.g. 31-02
        return None
    if when <= local_now:
        return None
    return when.astimezone(UTC)
