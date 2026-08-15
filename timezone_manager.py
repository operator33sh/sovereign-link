"""
timezone_manager.py — User timezone configuration for Sovereign-Link.

Stores the user's IANA timezone in vault/.system/timezone_config.json.
The scheduler uses this to interpret naive datetimes (e.g. "03:00") as
the user's local time rather than server time.

Uses the stdlib `zoneinfo` module (Python 3.9+). Install `tzdata` on
systems without a system timezone database (e.g. minimal Docker images).
"""
import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
_CONFIG_PATH = os.path.join(_VAULT_PATH, ".system", "timezone_config.json")
_DEFAULT_TZ = "Europe/Amsterdam"


def _config_path() -> str:
    """Allow tests to override VAULT_PATH at runtime."""
    vault = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
    return os.path.join(vault, ".system", "timezone_config.json")


def _load() -> dict:
    path = _config_path()
    if not os.path.exists(path):
        return {"user_timezone": _DEFAULT_TZ}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("timezone_manager: failed to load config")
        return {"user_timezone": _DEFAULT_TZ}


def _save(cfg: dict) -> None:
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        logger.exception("timezone_manager: failed to save config")


def get_zoneinfo() -> ZoneInfo:
    """Return the configured ZoneInfo object (falls back to Europe/Amsterdam)."""
    tz_name = _load().get("user_timezone", _DEFAULT_TZ)
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("timezone_manager: unknown timezone '%s', falling back to %s", tz_name, _DEFAULT_TZ)
        return ZoneInfo(_DEFAULT_TZ)


def get_timezone_name() -> str:
    return _load().get("user_timezone", _DEFAULT_TZ)


def set_timezone(tz_string: str) -> str:
    """Validate and store an IANA timezone string."""
    tz_string = tz_string.strip()
    try:
        zi = ZoneInfo(tz_string)
    except ZoneInfoNotFoundError:
        return (
            f"Onbekende tijdzone: '{tz_string}'. "
            "Gebruik een geldige IANA naam zoals 'Europe/Amsterdam' of 'America/New_York'."
        )

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(zi)
    offset = now_local.utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    h, m = divmod(abs(total_minutes), 60)
    offset_str = f"{sign}{h:02d}:{m:02d}"

    cfg = {
        "user_timezone": tz_string,
        "last_updated": now_utc.isoformat(),
        "offset_from_utc": offset_str,
        "offset_minutes": total_minutes,
    }
    _save(cfg)

    return (
        f"Tijdzone ingesteld op **{tz_string}** (UTC{offset_str}). "
        f"Lokale tijd: {now_local.strftime('%d-%m-%Y %H:%M')}."
    )


def get_timezone_info() -> str:
    """Return a human-readable summary of the current timezone config."""
    cfg = _load()
    tz_name = cfg.get("user_timezone", _DEFAULT_TZ)
    try:
        zi = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return f"Tijdzone: {tz_name} (ongeldig — fallback naar {_DEFAULT_TZ})"

    now_local = datetime.now(zi)
    offset = now_local.utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    h, m = divmod(abs(total_minutes), 60)
    offset_str = f"{sign}{h:02d}:{m:02d}"

    return (
        f"Tijdzone: **{tz_name}** (UTC{offset_str})\n"
        f"Lokale tijd: {now_local.strftime('%d-%m-%Y %H:%M:%S')}"
    )
