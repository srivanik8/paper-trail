"""The digest command, end to end through the CLI."""

import json
import types
from datetime import UTC, datetime

import httpx
import pytest

from papertrail.cli import main
from papertrail.store import Store

WHEN = 1767268800  # 2026-01-01T12:00:00Z


def hit(object_id: str, title: str, points: int = 100, url: str | None = None) -> dict:
    return {
        "objectID": object_id,
        "title": title,
        "url": url or f"https://arxiv.org/abs/2401.{object_id:0>5}",
        "points": points,
        "num_comments": 10,
        "created_at_i": WHEN,
        "author": "someone",
    }


@pytest.fixture
def web(monkeypatch):
    routes: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(request.url.host)
        return route(request) if route else httpx.Response(404, json={}, text="")

    real = httpx.Client
    monkeypatch.setattr(
        "httpx.Client", lambda *a, **k: real(transport=httpx.MockTransport(handler))
    )
    return routes


@pytest.fixture
def hn(web):
    hits: list[dict] = []
    web["hn.algolia.com"] = lambda request: httpx.Response(200, json={"hits": hits, "nbPages": 1})
    web["huggingface.co"] = lambda request: httpx.Response(200, json=[])
    web["export.arxiv.org"] = lambda request: httpx.Response(
        200, text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    )
    return hits


@pytest.fixture
def scores(monkeypatch):
    state = {"by_title": {}, "default": 8}

    def fake_scorer(store, model=None, reuse=True, **kwargs):
        from papertrail.scorer import Scorer as RealScorer
        from papertrail.scoring import Batch

        class FakeClient:
            def __init__(self):
                self.messages = types.SimpleNamespace(parse=self._parse)

            def _parse(self, **call):
                body = call["messages"][0]["content"]
                items = json.loads(body.split("<items>")[1].split("</items>")[0])
                return types.SimpleNamespace(
                    parsed_output=Batch.model_validate(
                        {
                            "scores": [
                                {
                                    "id": item["id"],
                                    "signal_score": state["by_title"].get(
                                        item["title"], state["default"]
                                    ),
                                    "category": "paper",
                                    "one_line": f"Summary of {item['title']}.",
                                    "hype_flags": [],
                                    "why": "Because.",
                                }
                                for item in items
                            ]
                        }
                    ),
                    usage=types.SimpleNamespace(
                        input_tokens=1000,
                        output_tokens=500,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=0,
                    ),
                )

        return RealScorer(store, client=FakeClient(), model=model or "m", reuse=reuse)

    monkeypatch.setattr("papertrail.cli.Scorer", fake_scorer)
    return state


@pytest.fixture
def resend(web):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg_1"})

    web["api.resend.com"] = handler
    return sent


def run_digest(tmp_path, *extra, db=None):
    return main(
        [
            "digest",
            "--db",
            db or str(tmp_path / "p.db"),
            "--no-check",
            "--out",
            str(tmp_path / "d.html"),
            *extra,
        ]
    )


# --- rendering --------------------------------------------------------------


def test_a_digest_is_written_to_disk(hn, scores, tmp_path, capsys):
    hn.append(hit("1", "Sparse autoencoders scale to frontier models"))

    assert run_digest(tmp_path) == 0
    body = (tmp_path / "d.html").read_text()

    assert "Sparse autoencoders scale to frontier models" in body
    assert "Summary of Sparse autoencoders" in body
    assert "written:" in capsys.readouterr().out


def test_the_subject_is_reported(hn, scores, tmp_path, capsys):
    hn.append(hit("1", "Sparse autoencoders scale to frontier models"))
    run_digest(tmp_path)
    assert "subject: paper-trail" in capsys.readouterr().out


def test_low_scoring_stories_are_left_out(hn, scores, tmp_path):
    scores["by_title"] = {"A minor LLM tweak": 2, "A real inference result": 9}
    hn.extend([hit("1", "A minor LLM tweak"), hit("2", "A real inference result")])

    run_digest(tmp_path)
    body = (tmp_path / "d.html").read_text()

    assert "A real inference result" in body
    assert "A minor LLM tweak" not in body


def test_the_limit_is_respected(hn, scores, tmp_path, capsys):
    hn.extend(hit(str(i), f"An LLM result number {i}") for i in range(8))

    run_digest(tmp_path, "--limit", "3")
    assert "digest: 3 stories" in capsys.readouterr().out


def test_the_output_path_is_created(hn, scores, tmp_path):
    hn.append(hit("1", "An LLM result"))
    nested = tmp_path / "deep" / "nested" / "digest.html"

    main(["digest", "--db", str(tmp_path / "p.db"), "--no-check", "--out", str(nested)])
    assert nested.exists()


def test_a_bad_window_is_a_usage_error(tmp_path, capsys):
    assert main(["digest", "--since", "banana", "--db", str(tmp_path / "p.db")]) == 2
    assert "could not parse window" in capsys.readouterr().err


# --- sending ----------------------------------------------------------------


def test_the_digest_is_emailed(hn, scores, resend, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    hn.append(hit("1", "Sparse autoencoders scale to frontier models"))

    assert run_digest(tmp_path, "--send", "--to", "me@example.com") == 0

    (payload,) = resend
    assert payload["to"] == ["me@example.com"]
    assert "Sparse autoencoders" in payload["subject"]
    assert "sent to me@example.com" in capsys.readouterr().out


def test_a_story_is_not_sent_twice(hn, scores, resend, tmp_path, monkeypatch):
    """The piece people skip, then wonder why the same story arrives all week."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    hn.append(hit("1", "Sparse autoencoders scale to frontier models"))
    db = str(tmp_path / "p.db")

    run_digest(tmp_path, "--send", "--to", "me@example.com", db=db)
    run_digest(tmp_path, "--send", "--to", "me@example.com", db=db)

    assert len(resend) == 1  # the second run had nothing new to send


def test_again_resends_what_already_went_out(hn, scores, resend, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    hn.append(hit("1", "Sparse autoencoders scale to frontier models"))
    db = str(tmp_path / "p.db")

    run_digest(tmp_path, "--send", "--to", "me@example.com", db=db)
    run_digest(tmp_path, "--send", "--again", "--to", "me@example.com", db=db)

    assert len(resend) == 2


def test_sending_records_what_went_out(hn, scores, resend, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    hn.append(hit("1", "Sparse autoencoders scale to frontier models"))
    db = str(tmp_path / "p.db")

    run_digest(tmp_path, "--send", "--to", "me@example.com", db=db)

    with Store(db) as store:
        rows = store.since(datetime(2020, 1, 1, tzinfo=UTC))
        assert any(store.was_sent(row["id"]) for row in rows)


def test_an_empty_digest_is_not_sent(hn, scores, resend, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    scores["default"] = 1
    hn.append(hit("1", "A trivial LLM tweak"))

    assert run_digest(tmp_path, "--send", "--to", "me@example.com") == 0
    assert resend == []
    assert "not sending" in capsys.readouterr().out


def test_an_empty_digest_can_be_sent_deliberately(hn, scores, resend, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    scores["default"] = 1
    hn.append(hit("1", "A trivial LLM tweak"))

    run_digest(tmp_path, "--send", "--empty-ok", "--to", "me@example.com")
    assert len(resend) == 1
    assert "nothing cleared the bar" in resend[0]["subject"]


def test_rendering_without_send_never_mails(hn, scores, resend, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    hn.append(hit("1", "An LLM result"))

    run_digest(tmp_path)
    assert resend == []


def test_a_missing_key_is_a_setup_error(hn, scores, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    hn.append(hit("1", "An LLM result"))

    assert run_digest(tmp_path, "--send", "--to", "me@example.com") == 2
    assert "RESEND_API_KEY" in capsys.readouterr().err


def test_a_rejected_send_exits_nonzero_and_records_nothing(hn, scores, web, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    web["api.resend.com"] = lambda request: httpx.Response(422, json={"message": "unverified"})
    hn.append(hit("1", "An LLM result"))
    db = str(tmp_path / "p.db")

    assert run_digest(tmp_path, "--send", "--to", "me@example.com", db=db) == 1

    with Store(db) as store:
        rows = store.since(datetime(2020, 1, 1, tzinfo=UTC))
        assert not any(store.was_sent(row["id"]) for row in rows)
