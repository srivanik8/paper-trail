from datetime import UTC, datetime, timedelta, timezone

import pytest

from papertrail.ids import ID_LENGTH, item_id
from papertrail.models import Item

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_item(**overrides) -> Item:
    fields = {
        "title": "A model that does a thing",
        "url": "https://example.com/post",
        "source": "hn",
        "published_at": WHEN,
        "raw_signal": 42.0,
    }
    fields.update(overrides)
    return Item(**fields)


def test_id_is_stable_and_derived_from_url():
    assert make_item().id == item_id("https://example.com/post")
    assert len(make_item().id) == ID_LENGTH


def test_same_url_from_different_sources_shares_an_id():
    hn = make_item(source="hn")
    reddit = make_item(source="reddit", title="Different headline", raw_signal=3.0)
    assert hn.id == reddit.id


def test_published_at_is_normalized_to_utc_on_construction():
    ist = timezone(timedelta(hours=5, minutes=30))
    item = make_item(published_at=datetime(2026, 1, 1, 17, 30, tzinfo=ist))
    assert item.published_at == WHEN
    assert item.published_at.tzinfo is UTC


def test_naive_timestamps_are_refused():
    with pytest.raises(ValueError, match="naive"):
        make_item(published_at=datetime(2026, 1, 1, 12, 0))


@pytest.mark.parametrize(("field", "value"), [("title", "   "), ("url", "")])
def test_empty_required_fields_are_refused(field, value):
    with pytest.raises(ValueError):
        make_item(**{field: value})


def test_to_dict_is_json_safe():
    payload = make_item().to_dict()
    assert payload["published_at"] == "2026-01-01T12:00:00Z"
    assert payload["primary_source_url"] is None
    assert payload["id"] == make_item().id
