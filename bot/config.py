"""Configuration from environment variables (python-dotenv loads a local .env first).

Every setting has one env var; see .env.example for the full list with comments.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROFILES = ("persistent", "github_actions", "dev")
GITHUB_ACTIONS_DEFAULT_RUNTIME_MIN = 340


class ConfigError(Exception):
    """A required setting is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    owner_id: int
    submitter_ids: frozenset[int]
    approver_ids: frozenset[int]
    approval_group_id: int
    storage_channel_id: int
    channel_id: int
    mongo_url: str
    mongo_db_name: str
    min_interval_min: float
    max_interval_min: float
    tz_name: str
    dup_window_days: int
    max_runtime_minutes: float
    runtime_profile: str
    notify_restart: bool
    data_dir: Path
    log_dir: Path
    log_level: str
    file_logging: bool
    album_wait_seconds: float = 2.5
    tick_seconds: float = 5.0
    lease_heartbeat_seconds: float = 30.0
    lease_expiry_seconds: float = 120.0
    max_retries: int = 5
    pending_reminder_min: float = 60.0
    anthropic_api_key: str = ""
    ai_model: str = ""
    daily_report_hour: int = 22

    # ---- roles -------------------------------------------------------------------------------
    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id == self.owner_id

    def can_submit(self, user_id: int | None) -> bool:
        return self.is_owner(user_id) or (user_id in self.submitter_ids)

    def can_approve(self, user_id: int | None) -> bool:
        return self.is_owner(user_id) or (user_id in self.approver_ids)

    def is_staff(self, user_id: int | None) -> bool:
        return self.can_submit(user_id) or self.can_approve(user_id)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)


# ---- parsing helpers -------------------------------------------------------------------------
def _get(env: Mapping[str, str], name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _int(env: Mapping[str, str], name: str, *, required: bool = False, default: int | None = None) -> int:
    raw = _get(env, name)
    if not raw:
        if required or default is None:
            raise ConfigError(f"{name} is required")
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _get(env, name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _get(env, name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


def parse_ids(raw: str) -> frozenset[int]:
    """'123, 456 789' -> {123, 456, 789}"""
    out: set[int] = set()
    for part in raw.replace(",", " ").split():
        try:
            out.add(int(part))
        except ValueError:
            raise ConfigError(f"invalid Telegram user id {part!r}") from None
    return frozenset(out)


def detect_profile(env: Mapping[str, str]) -> str:
    explicit = _get(env, "RUNTIME_PROFILE").lower()
    if explicit:
        if explicit not in PROFILES:
            raise ConfigError(f"RUNTIME_PROFILE must be one of {', '.join(PROFILES)}")
        return explicit
    if env.get("GITHUB_ACTIONS") == "true":
        return "github_actions"
    if Path("/.dockerenv").exists() or env.get("INVOCATION_ID"):  # Docker / systemd
        return "persistent"
    if sys.stdin is not None and sys.stdin.isatty():
        return "dev"
    return "persistent"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    token = _get(env, "BOT_TOKEN")
    if not token:
        raise ConfigError("BOT_TOKEN is required")
    owner = _int(env, "OWNER_ID", required=True)
    profile = detect_profile(env)

    min_iv = _float(env, "MIN_INTERVAL_MIN", 10)
    max_iv = _float(env, "MAX_INTERVAL_MIN", 15)
    if min_iv <= 0 or max_iv < min_iv:
        raise ConfigError("need 0 < MIN_INTERVAL_MIN <= MAX_INTERVAL_MIN")

    tz_name = _get(env, "TZ_NAME", "Asia/Kolkata")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"unknown TZ_NAME {tz_name!r}") from None

    runtime_raw = _get(env, "MAX_RUNTIME_MINUTES")
    if runtime_raw:
        max_runtime = _float(env, "MAX_RUNTIME_MINUTES", 0)
    else:
        max_runtime = GITHUB_ACTIONS_DEFAULT_RUNTIME_MIN if profile == "github_actions" else 0

    data_dir = Path(_get(env, "DATA_DIR", "data"))
    return Config(
        bot_token=token,
        owner_id=owner,
        submitter_ids=parse_ids(_get(env, "SUBMITTER_IDS")),
        approver_ids=parse_ids(_get(env, "APPROVER_IDS")),
        approval_group_id=_int(env, "APPROVAL_GROUP_ID", required=True),
        storage_channel_id=_int(env, "STORAGE_CHANNEL_ID", required=True),
        channel_id=_int(env, "CHANNEL_ID", required=True),
        mongo_url=_get(env, "MONGO_URL") or _get(env, "MONGO_URI"),
        mongo_db_name=_get(env, "MONGO_DB_NAME", "channel_poster_bot"),
        min_interval_min=min_iv,
        max_interval_min=max_iv,
        tz_name=tz_name,
        dup_window_days=_int(env, "DUP_WINDOW_DAYS", default=30),
        max_runtime_minutes=max_runtime,
        runtime_profile=profile,
        notify_restart=_bool(env, "NOTIFY_RESTART", False),
        data_dir=data_dir,
        log_dir=Path(_get(env, "LOG_DIR", "logs")),
        log_level=_get(env, "LOG_LEVEL", "DEBUG" if profile == "dev" else "INFO").upper(),
        file_logging=_bool(env, "FILE_LOGGING", profile != "github_actions"),
        album_wait_seconds=_float(env, "ALBUM_WAIT_SECONDS", 2.5),
        tick_seconds=_float(env, "TICK_SECONDS", 5),
        lease_heartbeat_seconds=_float(env, "LEASE_HEARTBEAT_SECONDS", 30),
        lease_expiry_seconds=_float(env, "LEASE_EXPIRY_SECONDS", 120),
        max_retries=_int(env, "MAX_RETRIES", default=5),
        pending_reminder_min=_float(env, "PENDING_REMINDER_MIN", 60),
        anthropic_api_key=_get(env, "ANTHROPIC_API_KEY"),
        ai_model=_get(env, "AI_MODEL"),
        daily_report_hour=_int(env, "DAILY_REPORT_HOUR", default=22),
    )
