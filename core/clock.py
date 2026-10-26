"""Resolve AstrBot's timezone independently of its time prompt setting."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def host_timezone(context) -> str | None:
    """None delegates conversion to the server's timezone at each message time."""
    zone = (context.get_config() or {}).get("timezone")
    if not isinstance(zone, str) or not zone:
        return None
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return zone
