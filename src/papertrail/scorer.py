"""Calling the model, and paying for it.

One request per batch of stories, with the rubric in a cached system prompt and
the items after the cache breakpoint. Structured output is used rather than
free-text JSON, so a malformed response is the API's problem and not a parser's.

Two things are recorded on every call and neither is optional. **Usage**,
because a digest that quietly costs a dollar a day is a digest you will turn
off; and **scores**, keyed by cluster, so a re-run of the same day reuses what
it already paid for instead of buying it twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anthropic

from .scoring import BATCH_SIZE, SYSTEM_PROMPT, Batch, Score, batches, build_prompt
from .store import Store
from .timeutil import utcnow

#: Default model. Change with --model; the rubric is written for a model that
#: can hold ten items in mind at once and be harsh about nine of them.
DEFAULT_MODEL = "claude-opus-5"

#: Per-million-token prices, for the cost line in the run summary. These are
#: list prices and go stale; they are a running estimate, not an invoice.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: Cached input is billed at roughly a tenth of the input rate.
CACHE_READ_DISCOUNT = 0.1

#: Enough for 25 scored items with their justifications, well clear of the cap.
MAX_TOKENS = 16000


@dataclass(slots=True)
class Usage:
    """What a scoring run consumed."""

    model: str = DEFAULT_MODEL
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, usage: Any) -> None:
        """Accumulate one response's usage block."""
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost(self, model: str | None = None) -> float | None:
        """Estimated dollars for this run, or ``None`` for an unpriced model."""
        prices = PRICES.get(model or self.model)
        if prices is None:
            return None
        input_price, output_price = prices
        billable_input = self.input_tokens + self.cache_write_tokens * 1.25
        return (
            billable_input * input_price
            + self.cache_read_tokens * input_price * CACHE_READ_DISCOUNT
            + self.output_tokens * output_price
        ) / 1_000_000


class Scorer:
    """Scores stories in batches, reusing anything already scored.

    Args:
        store: Where scores and usage are recorded.
        client: An Anthropic client. One is built from the environment if
            omitted, which requires credentials to be configured.
        model: Model id.
        batch_size: Stories per request.
        reuse: Skip stories already scored in the store. Turning this off
            re-buys scores you already have.
    """

    def __init__(
        self,
        store: Store,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        batch_size: int = BATCH_SIZE,
        reuse: bool = True,
    ) -> None:
        self.store = store
        self._client = client
        self.model = model
        self.batch_size = batch_size
        self.reuse = reuse
        self.usage = Usage(model=model)

    def _api(self) -> Any:
        """Return the client, building one from the environment on first use."""
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def score(self, stories: list[Any], now: datetime | None = None) -> dict[str, Score]:
        """Return a score per cluster id, for as many stories as could be scored.

        A batch that fails is recorded and skipped: the stories in it come back
        unscored rather than taking the digest down. Nothing here raises.
        """
        moment = now or utcnow()
        scores: dict[str, Score] = {}

        pending = []
        for story in stories:
            cached = self._cached(story.cluster.cluster_id) if self.reuse else None
            if cached is not None:
                scores[story.cluster.cluster_id] = cached
            else:
                pending.append(story)

        for batch in batches(pending, self.batch_size):
            for score in self._score_batch(batch, moment):
                scores[score.id] = score

        return scores

    def _cached(self, cluster_id: str) -> Score | None:
        """Return a previously recorded score for this cluster, if any."""
        row = self.store.cached_score(cluster_id)
        if row is None:
            return None
        try:
            return Score.model_validate_json(row)
        except Exception:  # noqa: BLE001 - a stale schema must not be fatal
            return None

    def _score_batch(self, batch: list[Any], now: datetime) -> list[Score]:
        """Score one batch, converting every failure into an empty result."""
        try:
            response = self._api().messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        # The rubric is identical on every request all month, so
                        # it is worth caching; the items sit after this point.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": build_prompt(batch)}],
                output_format=Batch,
            )
        except anthropic.APIError as exc:
            self.usage.errors.append(f"{type(exc).__name__}: {exc}")
            return []
        except Exception as exc:  # noqa: BLE001 - one bad batch must not stop the run
            self.usage.errors.append(f"{type(exc).__name__}: {exc}")
            return []

        self.usage.add(getattr(response, "usage", None))

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            self.usage.errors.append("no parsed output in response")
            return []

        expected = {story.cluster.cluster_id for story in batch}
        kept = []
        for score in parsed.scores:
            # A score for an id we did not ask about is not usable, and is the
            # shape a prompt-injected response would take.
            if score.id in expected:
                kept.append(score)
                self.store.record_score(score.id, score.model_dump_json(), now=now)
        return kept


def format_usage(usage: Usage, model: str | None = None) -> str:
    """One line describing what a scoring run cost."""
    if not usage.requests:
        return ""

    cost = usage.cost(model)
    parts = [
        f"scored: {usage.requests} request{'' if usage.requests == 1 else 's'}",
        f"{usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens} in",
        f"{usage.output_tokens} out",
    ]
    if usage.cache_read_tokens:
        parts.append(f"{usage.cache_read_tokens} cached")
    if cost is not None:
        parts.append(f"~${cost:.4f}")
    return ", ".join(parts)


def scores_to_json(scores: dict[str, Score]) -> str:
    """Serialize a score map, for debugging and for the record."""
    return json.dumps({key: json.loads(value.model_dump_json()) for key, value in scores.items()})
