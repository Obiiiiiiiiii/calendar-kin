"""Timezone helpers.

All date reasoning — resolving "tonight"/"Thursday" in chat, the ±24h
coverage window, picking a week start — uses the user's configured timezone
(KIN_TIMEZONE env var or the Settings page), never the server clock. Hosted
servers (Railway) run UTC, which would otherwise shift dates by hours.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def zone(tz_name: str | None) -> ZoneInfo:
    """The configured zone, falling back to UTC on a bad/missing name."""
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:  # noqa: BLE001 — unknown zone names must not crash a scan
        return ZoneInfo("UTC")


def is_valid_timezone(tz_name: str) -> bool:
    try:
        ZoneInfo(tz_name)
        return True
    except Exception:  # noqa: BLE001
        return False


def aware_now(tz_name: str | None) -> datetime:
    return datetime.now(zone(tz_name))


def local_now(tz_name: str | None) -> datetime:
    """Naive wall-clock "now" in the configured zone — comparable with the
    naive event datetimes used throughout reconciliation."""
    return aware_now(tz_name).replace(tzinfo=None)
