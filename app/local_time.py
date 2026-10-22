"""Calendar date in the app timezone. Used by the revision report only."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app


def local_today(now=None):
    """Date of `now` in APP_TIMEZONE. `now` defaults to the current UTC instant."""
    name = (current_app.config.get("APP_TIMEZONE") or "America/Los_Angeles").strip()
    tz = ZoneInfo(name or "America/Los_Angeles")
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()
