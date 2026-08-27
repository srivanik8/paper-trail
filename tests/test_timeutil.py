from datetime import UTC, datetime, timedelta, timezone

import pytest

from papertrail.timeutil import (
    from_unix,
    isoformat_utc,
    parse_iso,
    parse_since,
    to_utc,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("24h", timedelta(hours=24)),
        ("90m", timedelta(minutes=90)),
        ("7d", timedelta(days=7)),
        ("2w", timedelta(weeks=2)),
        ("30s", timedelta(seconds=30)),
        (" 12H ", timedelta(hours=12)),
    ],
)
def test_parse_since_accepts_windows(text, expected):
    assert parse_since(text) == expected


@pytest.mark.parametrize("text", ["", "24", "h", "-1d", "0h", "24 hours", "1.5h"])
def test_parse_since_rejects_junk(text):
    with pytest.raises(ValueError):
        parse_since(text)


def test_to_utc_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="naive"):
        to_utc(datetime(2026, 1, 1, 12, 0, 0))


def test_to_utc_converts_offset_to_utc():
    ist = timezone(timedelta(hours=5, minutes=30))
    converted = to_utc(datetime(2026, 1, 1, 12, 0, tzinfo=ist))
    assert converted == datetime(2026, 1, 1, 6, 30, tzinfo=UTC)
    assert converted.tzinfo is UTC


def test_parse_iso_handles_z_suffix_and_offsets():
    assert parse_iso("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_iso("2026-01-01T05:30:00+05:30") == datetime(2026, 1, 1, tzinfo=UTC)
    # No offset at all is read as UTC, per API convention.
    assert parse_iso("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)


def test_isoformat_utc_always_renders_z():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert isoformat_utc(datetime(2026, 1, 1, 12, 0, tzinfo=ist)) == "2026-01-01T06:30:00Z"


def test_from_unix_is_utc_aware():
    assert from_unix(0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert from_unix(0).tzinfo is UTC
