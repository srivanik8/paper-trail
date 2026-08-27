import json

from papertrail.cli import main


def test_bad_window_exits_with_a_usage_error(capsys):
    assert main(["run", "--since", "banana"]) == 2
    assert "could not parse window" in capsys.readouterr().err


def test_json_output_is_one_object_per_line(capsys, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from papertrail import cli
    from papertrail.models import Item
    from papertrail.pipeline import RunResult

    item = Item(
        title="An LLM thing",
        url="https://example.com/a",
        source="hn",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        raw_signal=12.5,
    )
    monkeypatch.setattr(
        cli,
        "run",
        lambda *a, **k: RunResult(
            items=[item],
            since=datetime(2026, 1, 1, tzinfo=UTC) - timedelta(hours=24),
            errors={},
        ),
    )

    assert main(["run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["title"] == "An LLM thing"
    assert payload["published_at"] == "2026-01-01T12:00:00Z"
