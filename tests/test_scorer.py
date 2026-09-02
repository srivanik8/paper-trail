from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import pytest

from papertrail.dedup import deduplicate
from papertrail.models import Item
from papertrail.pipeline import Story
from papertrail.provenance import classify
from papertrail.scorer import PRICES, Scorer, Usage, format_usage
from papertrail.scoring import Batch, Score
from papertrail.store import Store
from papertrail.substance import Substance

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def make_story(url: str = "https://arxiv.org/abs/2401.00001", title: str = "A paper") -> Story:
    item = Item(title=title, url=url, source="hn", published_at=NOW, raw_signal=100.0)
    (cluster,) = deduplicate([item])
    return Story(cluster=cluster, provenance=classify(url), substance=Substance())


def score_for(story: Story, value: int = 7) -> dict:
    return {
        "id": story.cluster.cluster_id,
        "signal_score": value,
        "category": "paper",
        "one_line": "A concrete result.",
        "hype_flags": [],
        "why": "Because.",
    }


class FakeClient:
    """Stands in for anthropic.Anthropic, recording what it was asked."""

    def __init__(self, responses: list, usage: dict | None = None):
        self._responses = list(responses)
        self._usage = usage or {
            "input_tokens": 1200,
            "output_tokens": 800,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._responses.pop(0) if self._responses else Batch(scores=[])
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(parsed_output=outcome, usage=SimpleNamespace(**self._usage))


# --- scoring ----------------------------------------------------------------


def test_a_story_is_scored_and_keyed_by_cluster(store):
    story = make_story()
    client = FakeClient([Batch.model_validate({"scores": [score_for(story, 8)]})])

    scores = Scorer(store, client=client).score([story], now=NOW)

    assert scores[story.cluster.cluster_id].signal_score == 8


def test_the_request_uses_the_configured_model_and_structured_output(store):
    story = make_story()
    client = FakeClient([Batch(scores=[])])
    Scorer(store, client=client, model="claude-sonnet-5").score([story], now=NOW)

    (call,) = client.calls
    assert call["model"] == "claude-sonnet-5"
    assert call["output_format"] is Batch


def test_the_rubric_is_sent_as_a_cached_system_prompt(store):
    """It is byte-identical on every request all month, so it should cache."""
    client = FakeClient([Batch(scores=[])])
    Scorer(store, client=client).score([make_story()], now=NOW)

    (system,) = client.calls[0]["system"]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert "established fact" in system["text"]


def test_adaptive_thinking_is_requested(store):
    client = FakeClient([Batch(scores=[])])
    Scorer(store, client=client).score([make_story()], now=NOW)
    assert client.calls[0]["thinking"] == {"type": "adaptive"}


def test_stories_are_split_across_requests(store):
    stories = [make_story(f"https://arxiv.org/abs/2401.{i:05d}") for i in range(5)]
    client = FakeClient([Batch(scores=[]), Batch(scores=[])])

    Scorer(store, client=client, batch_size=3).score(stories, now=NOW)
    assert len(client.calls) == 2


# --- reuse ------------------------------------------------------------------


def test_a_second_run_reuses_the_recorded_score_without_paying_again(store):
    story = make_story()
    client = FakeClient([Batch.model_validate({"scores": [score_for(story, 9)]})])
    scorer = Scorer(store, client=client)

    scorer.score([story], now=NOW)
    again = Scorer(store, client=FakeClient([])).score([story], now=NOW)

    assert again[story.cluster.cluster_id].signal_score == 9


def test_reuse_can_be_turned_off(store):
    story = make_story()
    first = FakeClient([Batch.model_validate({"scores": [score_for(story, 3)]})])
    Scorer(store, client=first).score([story], now=NOW)

    second = FakeClient([Batch.model_validate({"scores": [score_for(story, 9)]})])
    scores = Scorer(store, client=second, reuse=False).score([story], now=NOW)

    assert scores[story.cluster.cluster_id].signal_score == 9
    assert len(second.calls) == 1


def test_only_unscored_stories_are_sent(store):
    known, fresh = make_story(), make_story("https://arxiv.org/abs/2401.00002")
    Scorer(store, client=FakeClient([Batch.model_validate({"scores": [score_for(known)]})])).score(
        [known], now=NOW
    )

    client = FakeClient([Batch.model_validate({"scores": [score_for(fresh)]})])
    Scorer(store, client=client).score([known, fresh], now=NOW)

    assert fresh.cluster.cluster_id in client.calls[0]["messages"][0]["content"]
    assert known.cluster.cluster_id not in client.calls[0]["messages"][0]["content"]


def test_a_corrupt_cached_score_is_ignored_not_fatal(store):
    story = make_story()
    store.record_score(story.cluster.cluster_id, "{not json", now=NOW)

    client = FakeClient([Batch.model_validate({"scores": [score_for(story, 5)]})])
    scores = Scorer(store, client=client).score([story], now=NOW)
    assert scores[story.cluster.cluster_id].signal_score == 5


# --- failure ----------------------------------------------------------------


def test_an_api_error_leaves_the_batch_unscored_without_raising(store):
    request = anthropic._base_client  # noqa: SLF001 - only to build a real error object
    error = anthropic.APIConnectionError(request=SimpleNamespace(url="x"))
    scorer = Scorer(store, client=FakeClient([error]))

    assert scorer.score([make_story()], now=NOW) == {}
    assert scorer.usage.errors
    assert request is not None


def test_one_failing_batch_does_not_lose_the_others(store):
    stories = [make_story(f"https://arxiv.org/abs/2401.{i:05d}") for i in range(4)]
    good = Batch.model_validate({"scores": [score_for(stories[2]), score_for(stories[3])]})
    client = FakeClient([RuntimeError("boom"), good])

    scores = Scorer(store, client=client, batch_size=2).score(stories, now=NOW)
    assert len(scores) == 2


def test_a_response_with_no_parsed_output_is_recorded_as_an_error(store):
    class Empty(FakeClient):
        def _parse(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(parsed_output=None, usage=SimpleNamespace(**self._usage))

    scorer = Scorer(store, client=Empty([]))
    assert scorer.score([make_story()], now=NOW) == {}
    assert "no parsed output" in scorer.usage.errors[0]


def test_a_score_for_an_id_we_did_not_ask_about_is_discarded(store):
    """The shape a prompt-injected response would take."""
    story = make_story()
    smuggled = dict(score_for(story), id="not-a-cluster-we-sent")
    client = FakeClient([Batch.model_validate({"scores": [smuggled]})])

    assert Scorer(store, client=client).score([story], now=NOW) == {}


# --- usage ------------------------------------------------------------------


def test_usage_accumulates_across_requests(store):
    stories = [make_story(f"https://arxiv.org/abs/2401.{i:05d}") for i in range(4)]
    scorer = Scorer(store, client=FakeClient([Batch(scores=[]), Batch(scores=[])]), batch_size=2)
    scorer.score(stories, now=NOW)

    assert scorer.usage.requests == 2
    assert scorer.usage.input_tokens == 2400
    assert scorer.usage.output_tokens == 1600


def test_cost_uses_list_prices():
    usage = Usage(requests=1, input_tokens=1_000_000, output_tokens=1_000_000)
    input_price, output_price = PRICES["claude-opus-5"]
    assert usage.cost("claude-opus-5") == pytest.approx(input_price + output_price)


def test_cached_input_is_much_cheaper_than_fresh_input():
    fresh = Usage(requests=1, input_tokens=1_000_000)
    cached = Usage(requests=1, cache_read_tokens=1_000_000)
    assert cached.cost("claude-opus-5") < fresh.cost("claude-opus-5") / 5


def test_an_unpriced_model_reports_no_cost():
    assert Usage(requests=1, input_tokens=100).cost("some-future-model") is None


def test_the_usage_line_reports_tokens_and_an_estimate():
    line = format_usage(Usage(requests=2, input_tokens=5000, output_tokens=2000), "claude-opus-5")
    assert "2 requests" in line and "$" in line


def test_a_run_that_scored_nothing_prints_nothing():
    assert format_usage(Usage(), "claude-opus-5") == ""


def test_a_client_is_only_built_when_a_request_is_actually_made(store):
    """Constructing the SDK client needs credentials; scoring nothing must not."""
    scorer = Scorer(store)
    assert scorer.score([], now=NOW) == {}


def test_an_empty_story_list_makes_no_requests(store):
    client = FakeClient([])
    assert Scorer(store, client=client).score([], now=NOW) == {}
    assert client.calls == []


def test_scores_persist_across_store_connections(tmp_path):
    story = make_story()
    with Store(tmp_path / "p.db") as store:
        client = FakeClient([Batch.model_validate({"scores": [score_for(story, 6)]})])
        Scorer(store, client=client).score([story], now=NOW)

    with Store(tmp_path / "p.db") as store:
        payload = store.cached_score(story.cluster.cluster_id)
        assert Score.model_validate_json(payload).signal_score == 6
