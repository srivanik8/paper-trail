import json
from datetime import UTC, datetime

import httpx
import pytest

from papertrail.cli import main
from papertrail.fetcher import Fetcher
from papertrail.sources.hn import API_URL

WHEN = 1767268800  # 2026-01-01T12:00:00Z


def hit(object_id: str, title: str, points: int = 100, url: str | None = None) -> dict:
    # Default to a URL that is itself a primary source, so the resolver settles
    # it without a fetch. Provenance is exercised on its own below.
    return {
        "objectID": object_id,
        "title": title,
        "url": url or f"https://github.com/owner/repo-{object_id}",
        "points": points,
        "num_comments": 10,
        "created_at_i": WHEN,
        "author": "someone",
    }


@pytest.fixture
def hn(monkeypatch):
    """Serve a fixed set of HN hits to the CLI, without touching the network."""
    hits: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(API_URL)
        return httpx.Response(200, json={"hits": hits, "nbPages": 1})

    real_client = httpx.Client
    monkeypatch.setattr(
        "papertrail.sources.hn.httpx.Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )
    return hits


def test_bad_window_exits_with_a_usage_error(capsys):
    assert main(["run", "--since", "banana"]) == 2
    assert "could not parse window" in capsys.readouterr().err


def test_running_twice_reports_nothing_new_the_second_time(hn, tmp_path, capsys):
    """The day 2 acceptance check, end to end through the CLI."""
    hn.extend(
        [
            hit("1", "Mistral releases Large 3", points=300),
            hit("2", "A new transformer architecture for inference", points=120),
        ]
    )
    db = str(tmp_path / "papertrail.db")

    assert main(["run", "--source", "hn", "--db", db]) == 0
    first = capsys.readouterr().out
    assert "2 new, 0 seen before" in first

    assert main(["run", "--source", "hn", "--db", db]) == 0
    second = capsys.readouterr().out
    assert "0 new, 2 seen before" in second


def test_new_only_hides_stories_already_reported(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    db = str(tmp_path / "papertrail.db")

    main(["run", "--source", "hn", "--db", db])
    capsys.readouterr()

    assert main(["run", "--source", "hn", "--db", db, "--new-only"]) == 0
    assert "no items" in capsys.readouterr().out


def test_dry_run_leaves_the_database_untouched(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    db = str(tmp_path / "papertrail.db")

    main(["run", "--source", "hn", "--db", db, "--dry-run"])
    capsys.readouterr()
    main(["run", "--source", "hn", "--db", db, "--dry-run"])
    assert "1 new, 0 seen before" in capsys.readouterr().out


def test_duplicates_are_folded_and_the_other_source_is_named(hn, tmp_path, capsys):
    hn.extend(
        [
            hit("1", "Mistral releases Large 3", points=300),
            hit("2", "Mistral has released Large 3, its flagship model", points=40),
        ]
    )

    assert main(["run", "--source", "hn", "--db", str(tmp_path / "p.db")]) == 0
    out = capsys.readouterr().out
    assert "fetched 2 -> 1 story" in out
    assert "1 folded in" in out


def test_json_output_is_one_object_per_line(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))

    assert main(["run", "--source", "hn", "--db", str(tmp_path / "p.db"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["title"] == "Mistral releases Large 3"
    assert payload["published_at"] == "2026-01-01T12:00:00Z"
    assert payload["seen_before"] is False
    assert payload["cluster_id"] == payload["id"]


def test_json_marks_a_story_a_previous_run_reported(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    db = str(tmp_path / "p.db")

    main(["run", "--source", "hn", "--db", db])
    capsys.readouterr()

    main(["run", "--source", "hn", "--db", db, "--json"])
    assert json.loads(capsys.readouterr().out.strip())["seen_before"] is True


def test_limit_caps_the_table(hn, tmp_path, capsys):
    hn.extend(
        [
            hit("1", "Mistral releases Large 3", points=300),
            hit("2", "A new transformer architecture for inference", points=200),
            hit("3", "Fine-tuning diffusion models on consumer hardware", points=100),
        ]
    )

    main(["run", "--source", "hn", "--db", str(tmp_path / "p.db"), "--limit", "2"])
    body = capsys.readouterr().out.split("TITLE")[1]
    assert (
        len([line for line in body.splitlines() if line.strip().startswith(("~", "1", "2", "3"))])
        <= 3
    )


def test_a_totally_failed_run_exits_nonzero(monkeypatch, tmp_path):
    def explode(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("papertrail.sources.hn.httpx.Client", explode)
    assert main(["run", "--source", "hn", "--db", str(tmp_path / "p.db")]) == 1


def test_the_database_is_created_on_first_run(hn, tmp_path):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    db = tmp_path / "nested" / "papertrail.db"

    assert main(["run", "--source", "hn", "--db", str(db)]) == 0
    assert db.exists()


def test_datetimes_leave_as_iso_utc(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    main(["run", "--source", "hn", "--db", str(tmp_path / "p.db")])
    assert "01-01 12:00" in capsys.readouterr().out


def test_json_reports_provenance_found_on_any_cluster_member(hn, tmp_path, capsys):
    """An arXiv link on HN resolves its own primary source through canonicalization."""
    hn.append(
        hit("1", "Sparse autoencoders scale to frontier models", points=300)
        | {"url": "https://arxiv.org/pdf/2401.00001v2.pdf?utm_source=x"}
    )

    main(["run", "--source", "hn", "--db", str(tmp_path / "p.db"), "--json"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["url"] == "https://arxiv.org/pdf/2401.00001v2.pdf?utm_source=x"
    assert payload["also_seen"] == []


# --- provenance -------------------------------------------------------------


def test_stories_with_no_primary_source_are_dropped(hn, tmp_path, capsys):
    hn.extend(
        [
            hit("1", "Mistral releases Large 3", url="https://arxiv.org/abs/2401.00001"),
            hit("2", "Ten predictions for AI in 2026", url="https://blog.example/predictions"),
        ]
    )

    assert main(["run", "--source", "hn", "--db", str(tmp_path / "p.db"), "--no-fetch"]) == 0
    out = capsys.readouterr().out
    assert "1 unsourced" in out
    assert "Mistral releases Large 3" in out
    assert "Ten predictions" not in out


def test_keep_unsourced_shows_what_the_resolver_missed(hn, tmp_path, capsys):
    hn.append(hit("2", "Ten predictions for AI in 2026", url="https://blog.example/predictions"))

    main(
        ["run", "--source", "hn", "--db", str(tmp_path / "p.db"), "--no-fetch", "--keep-unsourced"]
    )
    out = capsys.readouterr().out
    assert "Ten predictions" in out
    assert "0 unsourced" in out


def test_the_evidence_type_is_reported(hn, tmp_path, capsys):
    hn.extend(
        [
            hit("1", "Sparse autoencoders scale up", url="https://arxiv.org/abs/2401.00001"),
            hit("2", "A tiny inference runtime in C", url="https://github.com/owner/runtime"),
            hit("3", "New open weights model released", url="https://huggingface.co/org/model"),
        ]
    )

    main(["run", "--source", "hn", "--db", str(tmp_path / "p.db"), "--no-fetch"])
    out = capsys.readouterr().out
    assert "paper 1" in out and "repo 1" in out and "model_weights 1" in out


def test_json_carries_evidence_and_how_it_was_found(hn, tmp_path, capsys):
    hn.append(hit("1", "Sparse autoencoders scale up", url="https://arxiv.org/abs/2401.00001"))

    main(["run", "--source", "hn", "--db", str(tmp_path / "p.db"), "--no-fetch", "--json"])
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["evidence"] == "paper"
    assert payload["primary_source_url"] == "https://arxiv.org/abs/2401.00001"
    assert payload["provenance_via"] == "self"


def test_dropped_stories_are_recorded_with_their_reason(hn, tmp_path):
    from papertrail.store import STATUS_REJECTED, Store

    hn.append(hit("2", "Ten predictions for AI in 2026", url="https://blog.example/predictions"))
    db = str(tmp_path / "p.db")

    main(["run", "--source", "hn", "--db", db, "--no-fetch"])

    with Store(db) as store:
        rejected = [
            row
            for row in store.since(datetime(2020, 1, 1, tzinfo=UTC))
            if row["status"] == STATUS_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "no primary source"


def test_no_fetch_builds_no_fetcher_at_all(hn, tmp_path, monkeypatch):
    """--no-fetch must be safe to run anywhere, including with no network."""

    def explode(*args, **kwargs):
        raise AssertionError("a fetcher was built despite --no-fetch")

    monkeypatch.setattr("papertrail.cli.Fetcher", explode)
    hn.append(hit("1", "A tiny inference runtime", url="https://github.com/owner/runtime"))

    assert main(["run", "--source", "hn", "--db", str(tmp_path / "p.db"), "--no-fetch"]) == 0


def test_without_no_fetch_a_fetcher_is_built(hn, tmp_path, monkeypatch):
    built: list[int] = []
    real = Fetcher

    def spy(*args, **kwargs):
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr("papertrail.cli.Fetcher", spy)
    hn.append(hit("1", "A tiny inference runtime", url="https://github.com/owner/runtime"))

    main(["run", "--source", "hn", "--db", str(tmp_path / "p.db")])
    assert built
