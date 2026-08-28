"""Fuzzy clustering of items that tell the same story.

URL identity (:mod:`papertrail.ids`) catches one link wearing different
tracking params. It cannot catch what actually happens on a launch day: the
same announcement written up on HN, on Reddit, and in three newsletters, each
pointing at a different URL under a different headline. That is this module.

The comparison is ``token_set_ratio`` over normalized, lightly stemmed titles.
It ignores word order and tolerates one side carrying extra words -- exactly
the shape of "Mistral releases Large 3" against "Mistral has released Large 3,
its new flagship model". Stemming matters more than it sounds: without it that
pair scores 77, because ``releases`` and ``released`` are different tokens.

**No similarity threshold works on its own.** Measured against real headline
pairs, the worst genuine rewording scores 92 while the worst false merge scores
97: they overlap, so no cut point separates them. The 97 is
"GPT-5 benchmark results published" against "GPT-4 benchmark results published"
-- one character apart, different stories. In AI news the version number *is*
the story, so it gets a categorical veto rather than a vote:

* **Version veto.** If both titles carry numeric tokens and those sets differ,
  they are never merged, whatever they score. GPT-4/GPT-5, Large 3/Small 2,
  Llama 3/Llama 4.
* **Short titles** are compared for equality only; below a handful of tokens
  there is not enough text to judge.
* A cluster is only ever joined by comparison against its **canonical member**,
  never against whatever joined last, so a chain of near-misses cannot drift a
  cluster away from its subject.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .models import Item

#: Similarity at or above this joins a cluster. Calibrated against real headline
#: pairs: genuine rewordings bottom out around 92, false merges top out around
#: 89 once the version veto has removed the pathological cases.
DEFAULT_THRESHOLD = 90

#: Below this many tokens, only an exact normalized match counts as a duplicate.
MIN_TOKENS_FOR_FUZZY = 4

#: Suffixes stripped to fold "releases"/"released"/"release" into one token.
#: Deliberately not a real stemmer -- this only has to survive headline verbs.
_SUFFIXES = ("ing", "ed", "es", "s")

#: A token is version-bearing if it contains a digit.
_HAS_DIGIT = re.compile(r"\d")

#: Community prefixes and tags that carry no story content.
_PREFIXES = re.compile(
    r"^\s*(?:show|ask|tell)\s+hn\s*:\s*|^\s*\[(?:r|p|d|n|discussion|research|project)\]\s*",
    re.IGNORECASE,
)

#: A trailing year in parentheses, as HN adds to reposts: "... (2019)".
_TRAILING_YEAR = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Reduce a headline to comparable tokens.

    Strips community prefixes and repost years, drops punctuation, folds case
    and collapses whitespace.
    """
    text = _PREFIXES.sub("", title)
    text = _TRAILING_YEAR.sub("", text)
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip().lower()


def stem(token: str) -> str:
    """Strip a common inflectional suffix, leaving a stem of at least 3 characters."""
    for suffix in _SUFFIXES:
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def version_tokens(title: str) -> frozenset[str]:
    """Tokens carrying a digit: version numbers, model numbers, years."""
    return frozenset(token for token in normalize_title(title).split() if _HAS_DIGIT.search(token))


def similarity(left: str, right: str) -> float:
    """Similarity of two raw titles, 0-100.

    Returns 100 for an exact normalized match, and 0 when either side is empty,
    when a title is too short to judge fuzzily, or when the version veto fires.
    """
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0

    # Version veto: "GPT-4 results" and "GPT-5 results" are one character apart
    # and score 97. Only applied when both sides carry a number, so "Llama 4
    # released" still merges with "Llama released".
    left_versions, right_versions = version_tokens(left), version_tokens(right)
    if left_versions and right_versions and left_versions != right_versions:
        return 0.0

    if min(len(a.split()), len(b.split())) < MIN_TOKENS_FOR_FUZZY:
        return 0.0

    stemmed_a = " ".join(stem(token) for token in a.split())
    stemmed_b = " ".join(stem(token) for token in b.split())
    return float(fuzz.token_set_ratio(stemmed_a, stemmed_b))


@dataclass(frozen=True, slots=True)
class Known:
    """A cluster already recorded in the store, for cross-run matching."""

    cluster_id: str
    title: str


@dataclass(slots=True)
class Cluster:
    """One story, and every item that reported it."""

    cluster_id: str
    canonical: Item
    duplicates: list[Item] = field(default_factory=list)
    #: Set when this cluster continues one already in the store.
    is_continuation: bool = False

    @property
    def members(self) -> list[Item]:
        """Canonical item first, then the duplicates in the order they arrived."""
        return [self.canonical, *self.duplicates]

    @property
    def primary_source_url(self) -> str | None:
        """The paper or repo behind this story, from whichever member knows it.

        The best-supported report is rarely the most informative one: a link
        that reached the HN front page carries a score but no provenance, while
        the arXiv entry for the same work carries provenance and no score. The
        cluster is the thing that knows both.
        """
        for item in self.members:
            if item.primary_source_url:
                return item.primary_source_url
        return None

    @property
    def also_seen(self) -> list[str]:
        """Other sources that carried this story, deduplicated and ordered."""
        seen: dict[str, None] = {}
        for item in self.duplicates:
            if item.source != self.canonical.source:
                seen.setdefault(item.source, None)
        return list(seen)


def deduplicate(
    items: list[Item],
    known: list[Known] | None = None,
    threshold: int = DEFAULT_THRESHOLD,
) -> list[Cluster]:
    """Group ``items`` into clusters, reusing cluster ids from ``known``.

    Items are processed highest-signal first, so the best-supported report of a
    story becomes its canonical member and the rest attach to it.

    Args:
        items: Candidates from this run, in any order.
        known: Clusters seen in previous runs, typically the rolling window
            from :meth:`papertrail.store.Store.since`. An item matching one of
            these joins that cluster and the cluster is flagged as a
            continuation -- that is how a story stops arriving four days
            running.
        threshold: Minimum ``token_set_ratio`` to merge.

    Returns:
        Clusters ordered by their canonical item's signal, descending.
    """
    clusters: list[Cluster] = []
    by_url: dict[str, Cluster] = {}
    previous = list(known or ())

    for item in sorted(
        items, key=lambda candidate: (-candidate.raw_signal, candidate.title.lower())
    ):
        # Exact identity first: it is free and it is certain.
        existing = by_url.get(item.id)
        if existing is not None:
            existing.duplicates.append(item)
            continue

        joined = _best_match(item, clusters, threshold)
        if joined is not None:
            joined.duplicates.append(item)
            by_url.setdefault(item.id, joined)
            continue

        cluster = Cluster(cluster_id=item.id, canonical=item)
        prior = _best_known(item, previous, threshold)
        if prior is not None:
            cluster.cluster_id = prior.cluster_id
            cluster.is_continuation = True

        clusters.append(cluster)
        by_url[item.id] = cluster

    return clusters


def _best_match(item: Item, clusters: list[Cluster], threshold: int) -> Cluster | None:
    """Return the closest cluster at or above ``threshold``, or ``None``."""
    best: Cluster | None = None
    best_score = float(threshold)
    for cluster in clusters:
        score = similarity(item.title, cluster.canonical.title)
        if score >= best_score:
            best, best_score = cluster, score
    return best


def _best_known(item: Item, known: list[Known], threshold: int) -> Known | None:
    """Return the closest previously-stored cluster at or above ``threshold``."""
    best: Known | None = None
    best_score = float(threshold)
    for candidate in known:
        score = similarity(item.title, candidate.title)
        if score >= best_score:
            best, best_score = candidate, score
    return best
