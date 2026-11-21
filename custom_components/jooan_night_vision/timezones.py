"""IANA time zones the JOOAN camera time-zone command can be given.

The vendor app sends the camera only a ``GMT+hh:mm`` string (command 66357);
the cloud ``timezone`` property mirrors it. The IANA ``timezone_id`` property
is written at provisioning and is not changed by that command, so Home
Assistant keeps the selected IANA name itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME_ZONES: tuple[str, ...] = (
    "Pacific/Midway", "Pacific/Honolulu", "America/Anchorage",
    "America/Los_Angeles", "America/Phoenix", "America/Denver",
    "America/Chicago", "America/Mexico_City", "America/New_York",
    "America/Bogota", "America/Caracas", "America/Halifax",
    "America/St_Johns", "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
    "Atlantic/South_Georgia", "Atlantic/Azores", "UTC", "Europe/London",
    "Europe/Lisbon", "Africa/Casablanca", "Europe/Paris", "Europe/Berlin",
    "Europe/Rome", "Europe/Madrid", "Europe/Amsterdam", "Europe/Stockholm",
    "Europe/Warsaw", "Europe/Athens", "Europe/Helsinki", "Europe/Kyiv",
    "Africa/Cairo", "Africa/Johannesburg", "Asia/Jerusalem", "Asia/Beirut",
    "Europe/Istanbul", "Europe/Moscow", "Asia/Baghdad", "Asia/Riyadh",
    "Africa/Nairobi", "Asia/Tehran", "Asia/Dubai", "Asia/Baku", "Asia/Kabul",
    "Asia/Tashkent", "Asia/Karachi", "Asia/Kolkata", "Asia/Kathmandu",
    "Asia/Dhaka", "Asia/Almaty", "Asia/Yangon", "Asia/Bangkok", "Asia/Jakarta",
    "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore", "Australia/Perth",
    "Asia/Tokyo", "Asia/Seoul", "Australia/Darwin", "Australia/Adelaide",
    "Australia/Sydney", "Australia/Brisbane", "Pacific/Guam", "Asia/Magadan",
    "Pacific/Auckland", "Pacific/Fiji",
)


def cloud_offset(zone: str, when: datetime | None = None) -> str:
    """Return the ``GMT+hh:mm`` string for ``zone`` at ``when`` (default now)."""
    if zone not in TIME_ZONES:
        raise ValueError("unsupported time zone")
    try:
        info = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as err:
        raise ValueError("unsupported time zone") from err
    moment = (when or datetime.now(timezone.utc)).astimezone(info)
    offset = moment.utcoffset()
    assert offset is not None
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"GMT{sign}{hours:02d}:{rest:02d}"
