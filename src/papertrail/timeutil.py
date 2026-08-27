"""Time handling for the pipeline.

Every timestamp that enters the system is converted to a timezone-aware UTC
``datetime`` at the boundary, and every timestamp that leaves it is rendered as
ISO-8601 with a ``Z`` suffix. Naive datetimes are rejected rather than guessed
at -- a silently-local timestamp is what breaks the rolling dedup window later.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)

_SINCE_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def utcnow() -> datetime:
    """Current time, timezone-aware, in UTC."""
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC.

    Raises:
        ValueError: if ``value`` is naive. We refuse to assume a timezone.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("naive datetime; timestamps must carry a timezone")
    return value.astimezone(UTC)


def from_unix(seconds: float) -> datetime:
    """Build a UTC datetime from a POSIX timestamp."""
    return datetime.fromtimestamp(seconds, tz=UTC)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string to UTC, accepting a trailing ``Z``.

    A string with no offset is treated as UTC: unlike a naive ``datetime``
    object, a bare ISO-8601 timestamp from an API is UTC by convention.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def isoformat_utc(value: datetime) -> str:
    """Render an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_since(value: str) -> timedelta:
    """Parse a lookback window such as ``24h``, ``90m``, ``7d``.

    Raises:
        ValueError: on an unparseable or non-positive window.
    """
    match = _SINCE_RE.match(value)
    if match is None:
        raise ValueError(f"could not parse window {value!r}; expected a form like 24h, 90m, 7d")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError(f"window must be positive, got {value!r}")
    return timedelta(**{_SINCE_UNITS[match.group(2).lower()]: amount})
