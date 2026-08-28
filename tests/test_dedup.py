from datetime import UTC, datetime

import pytest

from papertrail.dedup import (
    DEFAULT_THRESHOLD,
    Known,
    deduplicate,
    normalize_title,
    similarity,
    stem,
    version_tokens,
)
from papertrail.models import Item

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_item(title: str, url: str, signal: float = 10.0, source: str = "hn") -> Item:
    return Item(title=title, url=url, source=source, published_at=WHEN, raw_signal=signal)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Show HN: A tiny LLM runtime", "a tiny llm runtime"),
        ("Ask HN: best local model?", "best local model"),
        ("Tell HN: we shipped", "we shipped"),
        ("[R] Sparse autoencoders at scale", "sparse autoencoders at scale"),
        ("[D] Why RAG underperforms", "why rag underperforms"),
        ("The Bitter Lesson (2019)", "the bitter lesson"),
        ("  Mixed   CASE  and   spacing ", "mixed case and spacing"),
    ],
)
def test_normalize_strips_community_noise(raw, expected):
    assert normalize_title(raw) == expected


def test_identical_titles_score_full_marks():
    assert similarity("Show HN: A tiny LLM runtime", "A tiny LLM runtime") == 100.0


def test_rewordings_of_one_story_score_above_threshold():
    score = similarity(
        "Mistral releases Large 3",
        "Mistral has released Large 3, its new flagship model",
    )
    assert score >= DEFAULT_THRESHOLD


def test_short_titles_are_compared_for_equality_only():
    # Too little text to judge fuzzily: abstain rather than guess.
    assert similarity("GPT-5 out", "GPT-5 here") == 0.0
    # An exact match still counts, however short.
    assert similarity("GPT-5 is out", "GPT-5 is out!") == 100.0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # One character apart, different stories. Scores 97 on raw similarity.
        ("GPT-5 benchmark results published", "GPT-4 benchmark results published"),
        ("Mistral releases Large 3", "Mistral releases Small 2"),
        ("Llama 3 runs on a laptop", "Llama 4 runs on a laptop"),
    ],
)
def test_version_veto_blocks_merges_across_version_numbers(left, right):
    assert similarity(left, right) == 0.0


def test_version_veto_needs_a_number_on_both_sides():
    """ "Llama 4 released" and "Llama released" are not vetoed -- only one has a number."""
    assert similarity("Llama 4 model released today", "Llama model released today") > 0.0


def test_matching_version_numbers_do_not_veto():
    assert similarity("Mistral releases Large 3", "Mistral has released Large 3 model") >= (
        DEFAULT_THRESHOLD
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("releases", "releas"), ("released", "releas"), ("announcing", "announc"), ("is", "is")],
)
def test_stem_folds_headline_verb_inflections(raw, expected):
    assert stem(raw) == expected


def test_stemming_rescues_a_rewording_that_raw_matching_misses():
    """Without stemming this pair scores 77; releases/released are different tokens."""
    assert (
        similarity(
            "Mistral releases Large 3",
            "Mistral has released Large 3, its new flagship model",
        )
        >= DEFAULT_THRESHOLD
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("GPT-4 results", {"4"}),
        ("Llama 3.1 405B released", {"3", "1", "405b"}),
        ("No numbers at all here", set()),
    ],
)
def test_version_tokens_finds_digit_bearing_tokens(title, expected):
    assert version_tokens(title) == expected


def test_empty_titles_never_match():
    assert similarity("", "anything at all here") == 0.0


def test_unrelated_stories_stay_apart():
    assert (
        similarity(
            "Anthropic publishes interpretability results",
            "Postgres 18 adds asynchronous IO support",
        )
        < DEFAULT_THRESHOLD
    )


def test_one_story_across_three_sources_becomes_one_cluster():
    items = [
        make_item("Mistral releases Large 3", "https://hn.example/a", 300.0, "hn"),
        make_item("Mistral releases Large 3 today", "https://reddit.example/b", 90.0, "reddit"),
        make_item("Mistral has released Large 3 model", "https://news.example/c", 40.0, "rss"),
    ]
    (cluster,) = deduplicate(items)

    assert cluster.canonical.source == "hn"
    assert len(cluster.duplicates) == 2
    assert cluster.also_seen == ["reddit", "rss"]


def test_the_highest_signal_member_becomes_canonical():
    items = [
        make_item("Mistral releases Large 3", "https://a.example/x", 40.0),
        make_item("Mistral releases Large 3 model", "https://b.example/y", 300.0),
    ]
    (cluster,) = deduplicate(items)
    assert cluster.canonical.raw_signal == 300.0
    assert cluster.cluster_id == cluster.canonical.id


def test_distinct_stories_stay_in_distinct_clusters():
    items = [
        make_item("Mistral releases Large 3", "https://a.example/x"),
        make_item("Postgres 18 adds asynchronous IO", "https://b.example/y"),
    ]
    assert len(deduplicate(items)) == 2


def test_the_same_url_collapses_even_with_a_different_headline():
    items = [
        make_item("An editor's headline", "https://example.com/post", 5.0),
        make_item("A completely unrelated rewrite", "https://www.example.com/post/?ref=x", 50.0),
    ]
    (cluster,) = deduplicate(items)
    assert len(cluster.duplicates) == 1


def test_a_story_seen_in_an_earlier_run_continues_that_cluster():
    known = [Known(cluster_id="abc123", title="Mistral releases Large 3")]
    items = [make_item("Mistral has released Large 3 model", "https://new.example/z")]

    (cluster,) = deduplicate(items, known=known)
    assert cluster.cluster_id == "abc123"
    assert cluster.is_continuation is True


def test_a_genuinely_new_story_is_not_a_continuation():
    known = [Known(cluster_id="abc123", title="Mistral releases Large 3")]
    items = [make_item("Postgres 18 adds asynchronous IO", "https://new.example/z")]

    (cluster,) = deduplicate(items, known=known)
    assert cluster.is_continuation is False
    assert cluster.cluster_id == cluster.canonical.id


def test_members_lists_canonical_first():
    items = [
        make_item("Mistral releases Large 3", "https://a.example/x", 300.0),
        make_item("Mistral releases Large 3 today", "https://b.example/y", 10.0),
    ]
    (cluster,) = deduplicate(items)
    assert cluster.members[0] is cluster.canonical
    assert len(cluster.members) == 2


def test_also_seen_ignores_duplicates_from_the_canonical_source():
    items = [
        make_item("Mistral releases Large 3", "https://a.example/x", 300.0, "hn"),
        make_item("Mistral releases Large 3 today", "https://b.example/y", 10.0, "hn"),
    ]
    (cluster,) = deduplicate(items)
    assert cluster.also_seen == []


def test_clusters_come_back_ranked_by_signal():
    items = [
        make_item("Quiet story about embeddings", "https://a.example/x", 5.0),
        make_item("Loud story about transformers", "https://b.example/y", 900.0),
    ]
    assert [c.canonical.raw_signal for c in deduplicate(items)] == [900.0, 5.0]


def test_empty_input_yields_no_clusters():
    assert deduplicate([]) == []


def test_comparison_is_against_the_canonical_member_not_the_last_joiner():
    """A drifting chain must not walk a cluster away from its subject."""
    head = "Sparse autoencoder interpretability results scaling laws vision"
    middle = "Autoencoder interpretability results scaling laws vision transformer"
    tail = "Interpretability results scaling laws vision transformer training"

    assert similarity(head, middle) >= DEFAULT_THRESHOLD  # joins the head
    assert similarity(middle, tail) >= DEFAULT_THRESHOLD  # would chain onward
    assert similarity(head, tail) < DEFAULT_THRESHOLD  # but does not match the head

    clusters = deduplicate(
        [
            make_item(head, "https://a.example/1", 100.0),
            make_item(middle, "https://a.example/2", 90.0),
            make_item(tail, "https://a.example/3", 80.0),
        ]
    )
    assert len(clusters) == 2


def test_a_cluster_reports_a_primary_source_any_member_knows():
    """The top-ranked report carries the score; a quieter one carries the paper."""
    loud = Item(
        title="Sparse autoencoders scale to frontier models",
        url="https://news.example/story",
        source="hn",
        published_at=WHEN,
        raw_signal=320.0,
    )
    quiet = Item(
        title="Sparse autoencoders scale to frontier models",
        url="https://arxiv.org/abs/2401.00001",
        source="arxiv",
        published_at=WHEN,
        raw_signal=0.0,
        primary_source_url="https://arxiv.org/abs/2401.00001",
    )

    (cluster,) = deduplicate([loud, quiet])
    assert cluster.canonical is loud
    assert cluster.canonical.primary_source_url is None
    assert cluster.primary_source_url == "https://arxiv.org/abs/2401.00001"


def test_a_cluster_with_no_provenance_anywhere_reports_none():
    items = [make_item("Some story about agents and tools", "https://a.example/1")]
    assert deduplicate(items)[0].primary_source_url is None
