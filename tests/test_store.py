from datetime import UTC, datetime, timedelta

import pytest

from papertrail.models import Item
from papertrail.store import (
    SCHEMA_VERSION,
    STATUS_NEW,
    STATUS_REJECTED,
    STATUS_SENT,
    Store,
)

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def make_item(url: str = "https://example.com/post", **overrides) -> Item:
    fields = {
        "title": "An LLM that reads your logs",
        "url": url,
        "source": "hn",
        "published_at": WHEN,
        "raw_signal": 50.0,
    }
    fields.update(overrides)
    return Item(**fields)


def test_schema_version_is_recorded(store):
    assert store.schema_version == SCHEMA_VERSION


def test_first_upsert_is_new_and_second_is_not(store):
    item = make_item()
    assert store.upsert(item) is True
    assert store.upsert(item) is False


def test_repeat_sighting_within_the_same_second_is_not_new(store):
    """The timestamp trap: both rows carry the same stamp, so newness cannot use it."""
    item = make_item()
    assert store.upsert(item, now=WHEN) is True
    assert store.upsert(item, now=WHEN) is False


def test_urls_differing_only_by_tracking_collapse_to_one_row(store):
    assert store.upsert(make_item("https://example.com/post")) is True
    assert store.upsert(make_item("https://www.example.com/post/?utm_source=x")) is False
    assert store.counts_by_status() == {STATUS_NEW: 1}


def test_raw_signal_climbs_but_never_falls(store):
    store.upsert(make_item(raw_signal=50.0))
    store.upsert(make_item(raw_signal=180.0))
    assert store.get(make_item().id)["raw_signal"] == 180.0

    store.upsert(make_item(raw_signal=12.0))
    assert store.get(make_item().id)["raw_signal"] == 180.0


def test_first_seen_is_preserved_while_last_seen_advances(store):
    later = WHEN + timedelta(hours=6)
    store.upsert(make_item(), now=WHEN)
    store.upsert(make_item(), now=later)

    row = store.get(make_item().id)
    assert row["first_seen_at"] == "2026-01-01T12:00:00Z"
    assert row["last_seen_at"] == "2026-01-01T18:00:00Z"


def test_a_rejected_item_is_not_resurrected_by_re_ingestion(store):
    item = make_item()
    store.upsert(item, status=STATUS_REJECTED, reason="no primary source")
    store.upsert(item)

    row = store.get(item.id)
    assert row["status"] == STATUS_REJECTED
    assert row["reason"] == "no primary source"


def test_a_resolved_primary_source_is_never_lost(store):
    store.upsert(make_item(primary_source_url="https://arxiv.org/abs/2401.00001"))
    store.upsert(make_item(primary_source_url=None))
    assert store.get(make_item().id)["primary_source_url"] == "https://arxiv.org/abs/2401.00001"


def test_rejects_are_retained_with_their_reason(store):
    store.upsert(make_item(), status=STATUS_REJECTED, reason="no primary source")
    row = store.get(make_item().id)
    assert row["status"] == STATUS_REJECTED
    assert row["reason"] == "no primary source"


def test_extra_survives_the_json_round_trip(store):
    store.upsert(make_item(extra={"points": 87, "matched_terms": ["llm"]}))
    payload = Store.row_to_dict(store.get(make_item().id))
    assert payload["extra"] == {"points": 87, "matched_terms": ["llm"]}
    assert payload["published_at"] == WHEN


def test_cluster_id_defaults_to_the_item_id(store):
    item = make_item()
    store.upsert(item)
    assert store.get(item.id)["cluster_id"] == item.id


def test_cluster_id_can_be_shared_across_items(store):
    lead = make_item("https://example.com/a")
    follower = make_item("https://example.com/b", title="Same story elsewhere")
    store.upsert(lead)
    store.upsert(follower, cluster_id=lead.id)
    assert store.get(follower.id)["cluster_id"] == lead.id


def test_since_returns_only_items_first_seen_in_the_window(store):
    store.upsert(make_item("https://example.com/old"), now=WHEN - timedelta(days=30))
    store.upsert(make_item("https://example.com/new"), now=WHEN)

    rows = store.since(WHEN - timedelta(days=7))
    assert [row["url"] for row in rows] == ["https://example.com/new"]


def test_mark_sent_records_the_digest_and_flips_status(store):
    item = make_item()
    store.upsert(item)
    assert store.mark_sent([item.id], "2026-01-01") == 1
    assert store.was_sent(item.id) is True
    assert store.get(item.id)["status"] == STATUS_SENT


def test_resending_the_same_digest_is_a_no_op(store):
    item = make_item()
    store.upsert(item)
    store.mark_sent([item.id], "2026-01-01")
    assert store.mark_sent([item.id], "2026-01-01") == 0


def test_the_same_item_can_appear_in_two_different_digests(store):
    item = make_item()
    store.upsert(item)
    store.mark_sent([item.id], "2026-01-01")
    assert store.mark_sent([item.id], "2026-01-02") == 1


def test_unsent_items_report_as_unsent(store):
    store.upsert(make_item())
    assert store.was_sent(make_item().id) is False


def test_set_status_records_a_reason(store):
    item = make_item()
    store.upsert(item)
    store.set_status(item.id, STATUS_REJECTED, "below signal floor")
    row = store.get(item.id)
    assert (row["status"], row["reason"]) == (STATUS_REJECTED, "below signal floor")


def test_missing_items_return_none(store):
    assert store.get("deadbeefdeadbeef") is None
    assert store.has("deadbeefdeadbeef") is False


def test_a_file_backed_store_persists_across_connections(tmp_path):
    path = tmp_path / "nested" / "papertrail.db"
    with Store(path) as first:
        first.upsert(make_item())
    with Store(path) as second:
        assert second.has(make_item().id) is True
        assert second.upsert(make_item()) is False
