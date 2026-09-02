import json
from datetime import UTC, datetime

import httpx
import pytest

from papertrail.cli import main
from papertrail.fetcher import Fetcher

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
def web(monkeypatch):
    """Route every outbound request by host.

    All these modules share one ``httpx`` module object, so patching
    ``httpx.Client`` per-module means the last fixture applied wins for
    everything. One router keyed on hostname is the only arrangement that lets
    the HN feed and the GitHub API be mocked in the same test.
    """
    routes: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(request.url.host)
        if route is None:
            return httpx.Response(404, json={}, text="")
        return route(request)

    real = httpx.Client
    monkeypatch.setattr(
        "httpx.Client", lambda *a, **k: real(transport=httpx.MockTransport(handler))
    )
    return routes


@pytest.fixture
def hn(web):
    """Serve a fixed set of HN hits to the CLI, without touching the network."""
    hits: list[dict] = []
    web["hn.algolia.com"] = lambda request: httpx.Response(200, json={"hits": hits, "nbPages": 1})
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

    assert main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db]) == 0
    first = capsys.readouterr().out
    assert "2 new, 0 seen before" in first

    assert main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db]) == 0
    second = capsys.readouterr().out
    assert "0 new, 2 seen before" in second


def test_new_only_hides_stories_already_reported(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    db = str(tmp_path / "papertrail.db")

    main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db])
    capsys.readouterr()

    assert (
        main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db, "--new-only"]) == 0
    )
    assert "no items" in capsys.readouterr().out


def test_dry_run_leaves_the_database_untouched(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    db = str(tmp_path / "papertrail.db")

    main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db, "--dry-run"])
    capsys.readouterr()
    main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db, "--dry-run"])
    assert "1 new, 0 seen before" in capsys.readouterr().out


def test_duplicates_are_folded_and_the_other_source_is_named(hn, tmp_path, capsys):
    hn.extend(
        [
            hit("1", "Mistral releases Large 3", points=300),
            hit("2", "Mistral has released Large 3, its flagship model", points=40),
        ]
    )

    assert (
        main(["run", "--source", "hn", "--no-check", "--no-score", "--db", str(tmp_path / "p.db")])
        == 0
    )
    out = capsys.readouterr().out
    assert "fetched 2 -> 1 story" in out
    assert "1 folded in" in out


def test_json_output_is_one_object_per_line(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))

    assert (
        main(
            [
                "run",
                "--source",
                "hn",
                "--no-check",
                "--no-score",
                "--db",
                str(tmp_path / "p.db"),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["title"] == "Mistral releases Large 3"
    assert payload["published_at"] == "2026-01-01T12:00:00Z"
    assert payload["seen_before"] is False
    assert payload["cluster_id"] == payload["id"]


def test_json_marks_a_story_a_previous_run_reported(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    db = str(tmp_path / "p.db")

    main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db])
    capsys.readouterr()

    main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db, "--json"])
    assert json.loads(capsys.readouterr().out.strip())["seen_before"] is True


def test_limit_caps_the_table(hn, tmp_path, capsys):
    hn.extend(
        [
            hit("1", "Mistral releases Large 3", points=300),
            hit("2", "A new transformer architecture for inference", points=200),
            hit("3", "Fine-tuning diffusion models on consumer hardware", points=100),
        ]
    )

    main(
        [
            "run",
            "--source",
            "hn",
            "--no-check",
            "--no-score",
            "--db",
            str(tmp_path / "p.db"),
            "--limit",
            "2",
        ]
    )
    body = capsys.readouterr().out.split("TITLE")[1]
    assert (
        len([line for line in body.splitlines() if line.strip().startswith(("~", "1", "2", "3"))])
        <= 3
    )


def test_a_totally_failed_run_exits_nonzero(monkeypatch, tmp_path):
    def explode(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("papertrail.sources.hn.httpx.Client", explode)
    assert (
        main(["run", "--source", "hn", "--no-check", "--no-score", "--db", str(tmp_path / "p.db")])
        == 1
    )


def test_the_database_is_created_on_first_run(hn, tmp_path):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    db = tmp_path / "nested" / "papertrail.db"

    assert main(["run", "--source", "hn", "--no-check", "--no-score", "--db", str(db)]) == 0
    assert db.exists()


def test_datetimes_leave_as_iso_utc(hn, tmp_path, capsys):
    hn.append(hit("1", "Mistral releases Large 3", points=300))
    main(["run", "--source", "hn", "--no-check", "--no-score", "--db", str(tmp_path / "p.db")])
    assert "01-01 12:00" in capsys.readouterr().out


def test_json_reports_provenance_found_on_any_cluster_member(hn, tmp_path, capsys):
    """An arXiv link on HN resolves its own primary source through canonicalization."""
    hn.append(
        hit("1", "Sparse autoencoders scale to frontier models", points=300)
        | {"url": "https://arxiv.org/pdf/2401.00001v2.pdf?utm_source=x"}
    )

    main(
        [
            "run",
            "--source",
            "hn",
            "--no-check",
            "--no-score",
            "--db",
            str(tmp_path / "p.db"),
            "--json",
        ]
    )
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

    assert (
        main(
            [
                "run",
                "--source",
                "hn",
                "--no-check",
                "--no-score",
                "--db",
                str(tmp_path / "p.db"),
                "--no-fetch",
            ]
        )
        == 0
    )
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

    main(
        [
            "run",
            "--source",
            "hn",
            "--no-check",
            "--no-score",
            "--db",
            str(tmp_path / "p.db"),
            "--no-fetch",
        ]
    )
    out = capsys.readouterr().out
    assert "paper 1" in out and "repo 1" in out and "model_weights 1" in out


def test_json_carries_evidence_and_how_it_was_found(hn, tmp_path, capsys):
    hn.append(hit("1", "Sparse autoencoders scale up", url="https://arxiv.org/abs/2401.00001"))

    main(
        [
            "run",
            "--source",
            "hn",
            "--no-check",
            "--db",
            str(tmp_path / "p.db"),
            "--no-fetch",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["evidence"] == "paper"
    assert payload["primary_source_url"] == "https://arxiv.org/abs/2401.00001"
    assert payload["provenance_via"] == "self"


def test_dropped_stories_are_recorded_with_their_reason(hn, tmp_path):
    from papertrail.store import STATUS_REJECTED, Store

    hn.append(hit("2", "Ten predictions for AI in 2026", url="https://blog.example/predictions"))
    db = str(tmp_path / "p.db")

    main(["run", "--source", "hn", "--no-check", "--no-score", "--db", db, "--no-fetch"])

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

    assert (
        main(
            [
                "run",
                "--source",
                "hn",
                "--no-check",
                "--no-score",
                "--db",
                str(tmp_path / "p.db"),
                "--no-fetch",
            ]
        )
        == 0
    )


def test_without_no_fetch_a_fetcher_is_built(hn, tmp_path, monkeypatch):
    built: list[int] = []
    real = Fetcher

    def spy(*args, **kwargs):
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr("papertrail.cli.Fetcher", spy)
    hn.append(hit("1", "A tiny inference runtime", url="https://github.com/owner/runtime"))

    main(["run", "--source", "hn", "--no-check", "--no-score", "--db", str(tmp_path / "p.db")])
    assert built


# --- reality checks ---------------------------------------------------------


@pytest.fixture
def gh(web):
    """Serve GitHub API responses to the checker."""
    state = {"code_files": 2, "contributors": 47, "readme": "Build with cmake.", "span_days": 800}

    def handler(request: httpx.Request) -> httpx.Response:
        import base64
        from datetime import timedelta

        url = str(request.url)
        now = datetime(2026, 6, 1, tzinfo=UTC)

        if "/contributors" in url:
            return httpx.Response(
                200,
                json=[{"login": "a"}],
                headers={"link": f'<...&page={state["contributors"]}>; rel="last"'},
            )
        if "/commits" in url:
            if "page=2" in url:
                oldest = (now - timedelta(days=state["span_days"])).isoformat()
                return httpx.Response(200, json=[{"commit": {"committer": {"date": oldest}}}])
            return httpx.Response(
                200,
                json=[{"commit": {"committer": {"date": now.isoformat()}}}],
                headers={"link": '<...&page=2>; rel="last"'},
            )
        if "/git/trees/" in url:
            tree = [{"type": "blob", "path": "README.md"}]
            tree += [{"type": "blob", "path": f"src/f{i}.py"} for i in range(state["code_files"])]
            return httpx.Response(200, json={"tree": tree})
        if url.endswith("/readme"):
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(state["readme"].encode()).decode(),
                },
            )
        return httpx.Response(
            200,
            json={
                "full_name": "owner/repo",
                "stargazers_count": 4200,
                "created_at": "2024-01-01T00:00:00Z",
                "pushed_at": now.isoformat(),
                "default_branch": "main",
                "license": {"spdx_id": "MIT"},
            },
        )

    web["api.github.com"] = handler
    return state


def test_a_solid_repository_raises_no_flags(hn, gh, tmp_path, capsys):
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/owner/repo"))

    assert (
        main(["run", "--source", "hn", "--no-fetch", "--no-score", "--db", str(tmp_path / "p.db")])
        == 0
    )
    out = capsys.readouterr().out
    assert "flags:" not in out
    assert "thin:" not in out


def test_a_readme_only_repository_is_flagged_and_marked_thin(hn, gh, tmp_path, capsys):
    gh["code_files"] = 0
    hn.append(hit("1", "A revolutionary agent framework", url="https://github.com/owner/repo"))

    main(["run", "--source", "hn", "--no-fetch", "--no-score", "--db", str(tmp_path / "p.db")])
    out = capsys.readouterr().out

    assert "readme_only 1" in out
    assert "thin: 1 of 1" in out


def test_a_waitlist_readme_is_caught(hn, gh, tmp_path, capsys):
    gh["readme"] = "Join the waitlist for early access."
    hn.append(hit("1", "An agent that does everything", url="https://github.com/owner/repo"))

    main(["run", "--source", "hn", "--no-fetch", "--no-score", "--db", str(tmp_path / "p.db")])
    assert "waitlist 1" in capsys.readouterr().out


def test_a_thin_story_is_annotated_not_dropped(hn, gh, tmp_path, capsys):
    """Provenance drops; substance only annotates."""
    gh["code_files"] = 0
    hn.append(hit("1", "A revolutionary agent framework", url="https://github.com/owner/repo"))

    main(["run", "--source", "hn", "--no-fetch", "--no-score", "--db", str(tmp_path / "p.db")])
    out = capsys.readouterr().out

    assert "0 unsourced" in out
    assert "A revolutionary agent framework" in out


def test_json_carries_the_flags_and_velocity(hn, gh, tmp_path, capsys):
    gh["code_files"] = 0
    gh["contributors"] = 1
    hn.append(hit("1", "A revolutionary agent framework", url="https://github.com/owner/repo"))

    main(
        [
            "run",
            "--source",
            "hn",
            "--no-fetch",
            "--no-score",
            "--json",
            "--db",
            str(tmp_path / "p.db"),
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip())

    assert "readme_only" in payload["substance_flags"]
    assert "single_contributor" in payload["substance_flags"]
    assert payload["thin"] is True
    assert payload["star_velocity"] > 0


def test_flags_are_recorded_in_the_store(hn, gh, tmp_path):
    import json as jsonlib

    from papertrail.store import Store

    gh["code_files"] = 0
    hn.append(hit("1", "A revolutionary agent framework", url="https://github.com/owner/repo"))
    db = str(tmp_path / "p.db")

    main(["run", "--source", "hn", "--no-fetch", "--no-score", "--db", db])

    with Store(db) as store:
        (row,) = store.since(datetime(2020, 1, 1, tzinfo=UTC))
        assert "readme_only" in jsonlib.loads(row["substance_flags"])
        assert row["star_velocity"] is not None


def test_no_check_makes_no_api_calls(hn, tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a checker was built despite --no-check")

    monkeypatch.setattr("papertrail.cli.Checker", explode)
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/owner/repo"))

    assert (
        main(["run", "--source", "hn", "--no-check", "--no-fetch", "--db", str(tmp_path / "p.db")])
        == 0
    )


def test_unsourced_stories_are_never_checked(hn, gh, tmp_path, capsys):
    """No point spending an API call on something already dropped."""
    hn.append(
        hit("1", "Ten predictions for AI agents in 2026", url="https://blog.example/predictions")
    )

    main(["run", "--source", "hn", "--no-fetch", "--no-score", "--db", str(tmp_path / "p.db")])
    out = capsys.readouterr().out
    assert "1 unsourced" in out
    assert "api calls" not in out


# --- scoring ----------------------------------------------------------------


@pytest.fixture
def scores(monkeypatch):
    """Stand in for the model, returning a score for every item it is sent."""
    state = {"by_title": {}, "default": 5, "calls": []}

    def fake_scorer(store, model=None, reuse=True, **kwargs):
        from papertrail.scorer import Scorer as RealScorer
        from papertrail.scoring import Batch

        class FakeClient:
            def __init__(self):
                import types

                self.messages = types.SimpleNamespace(parse=self._parse)

            def _parse(self, **call):
                import json as jsonlib
                import types

                state["calls"].append(call)
                body = call["messages"][0]["content"]
                items = jsonlib.loads(body.split("<items>")[1].split("</items>")[0])
                return types.SimpleNamespace(
                    parsed_output=Batch.model_validate(
                        {
                            "scores": [
                                {
                                    "id": item["id"],
                                    "signal_score": state["by_title"].get(
                                        item["title"], state["default"]
                                    ),
                                    "category": "tool",
                                    "one_line": f"Summary of {item['title'][:40]}.",
                                    "hype_flags": ["vendor_benchmark"]
                                    if item["substance_flags"]
                                    else [],
                                    "why": "Because.",
                                }
                                for item in items
                            ]
                        }
                    ),
                    usage=types.SimpleNamespace(
                        input_tokens=1500,
                        output_tokens=900,
                        cache_read_input_tokens=1200,
                        cache_creation_input_tokens=0,
                    ),
                )

        return RealScorer(store, client=FakeClient(), model=model or "claude-opus-5", reuse=reuse)

    monkeypatch.setattr("papertrail.cli.Scorer", fake_scorer)
    return state


def test_stories_are_ranked_by_score_not_popularity(hn, scores, tmp_path, capsys):
    """The whole point: what got attention is not what deserved it."""
    scores["by_title"] = {"A loud but shallow agent launch": 2, "A quiet but real LLM result": 9}
    hn.extend(
        [
            hit(
                "1", "A loud but shallow agent launch", points=900, url="https://github.com/a/loud"
            ),
            hit(
                "2",
                "A quiet but real LLM result",
                points=30,
                url="https://arxiv.org/abs/2401.00001",
            ),
        ]
    )

    main(["run", "--source", "hn", "--no-fetch", "--no-check", "--db", str(tmp_path / "p.db")])
    out = capsys.readouterr().out

    body = out.split("TITLE")[1]
    assert body.index("A quiet but real LLM result") < body.index("A loud but shallow agent launch")


def test_the_score_and_one_line_are_shown(hn, scores, tmp_path, capsys):
    scores["default"] = 8
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/a/b"))

    main(["run", "--source", "hn", "--no-fetch", "--no-check", "--db", str(tmp_path / "p.db")])
    out = capsys.readouterr().out

    assert " 8  " in out
    assert "Summary of A fast inference runtime" in out


def test_the_cost_of_a_run_is_reported(hn, scores, tmp_path, capsys):
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/a/b"))

    main(["run", "--source", "hn", "--no-fetch", "--no-check", "--db", str(tmp_path / "p.db")])
    out = capsys.readouterr().out

    assert "scored: 1 request" in out
    assert "$" in out
    assert "cached" in out


def test_a_second_run_reuses_scores_and_makes_no_request(hn, scores, tmp_path, capsys):
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/a/b"))
    db = str(tmp_path / "p.db")

    main(["run", "--source", "hn", "--no-fetch", "--no-check", "--db", db])
    capsys.readouterr()
    made = len(scores["calls"])

    main(["run", "--source", "hn", "--no-fetch", "--no-check", "--db", db])
    assert len(scores["calls"]) == made
    assert "scored:" not in capsys.readouterr().out


def test_rescore_pays_again(hn, scores, tmp_path, capsys):
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/a/b"))
    db = str(tmp_path / "p.db")

    main(["run", "--source", "hn", "--no-fetch", "--no-check", "--db", db])
    made = len(scores["calls"])
    main(["run", "--source", "hn", "--no-fetch", "--no-check", "--rescore", "--db", db])
    assert len(scores["calls"]) > made


def test_json_carries_the_score_and_its_justification(hn, scores, tmp_path, capsys):
    scores["default"] = 6
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/a/b"))

    main(
        [
            "run",
            "--source",
            "hn",
            "--no-fetch",
            "--no-check",
            "--json",
            "--db",
            str(tmp_path / "p.db"),
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["signal_score"] == 6
    assert payload["category"] == "tool"
    assert payload["one_line"].startswith("Summary of")
    assert payload["why"] == "Because."


def test_the_model_can_be_chosen(hn, scores, tmp_path):
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/a/b"))
    main(
        [
            "run",
            "--source",
            "hn",
            "--no-fetch",
            "--no-check",
            "--model",
            "claude-sonnet-5",
            "--db",
            str(tmp_path / "p.db"),
        ]
    )
    assert scores["calls"][0]["model"] == "claude-sonnet-5"


def test_no_score_ranks_by_popularity_and_spends_nothing(hn, scores, tmp_path, capsys):
    hn.extend(
        [
            hit(
                "1", "A loud but shallow agent launch", points=900, url="https://github.com/a/loud"
            ),
            hit(
                "2",
                "A quiet but real LLM result",
                points=30,
                url="https://arxiv.org/abs/2401.00001",
            ),
        ]
    )

    main(
        [
            "run",
            "--source",
            "hn",
            "--no-fetch",
            "--no-check",
            "--no-score",
            "--db",
            str(tmp_path / "p.db"),
        ]
    )
    out = capsys.readouterr().out

    assert scores["calls"] == []
    body = out.split("TITLE")[1]
    assert body.index("A loud but shallow agent launch") < body.index("A quiet but real LLM result")


def test_scores_are_recorded_in_the_store(hn, scores, tmp_path):
    import json as jsonlib

    from papertrail.store import Store

    scores["default"] = 7
    hn.append(hit("1", "A fast inference runtime", url="https://github.com/a/b"))
    db = str(tmp_path / "p.db")

    main(["run", "--source", "hn", "--no-fetch", "--no-check", "--db", db])

    with Store(db) as store:
        (row,) = store.since(datetime(2020, 1, 1, tzinfo=UTC))
        assert row["signal_score"] == 7
        assert row["one_line"].startswith("Summary of")
        assert jsonlib.loads(row["hype_flags"]) == []
