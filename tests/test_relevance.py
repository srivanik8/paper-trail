import pytest

from papertrail.relevance import is_relevant, matched_terms


@pytest.mark.parametrize(
    "title",
    [
        "Show HN: An LLM that reads your logs",
        "Anthropic releases a new model",
        "Fine-tuning Llama on a single GPU",
        "AI is eating the world",
        "A new transformer architecture",
        "arXiv paper on diffusion models",
        "Building agents with RAG",
    ],
)
def test_relevant_titles_match(title):
    assert is_relevant(title)


@pytest.mark.parametrize(
    "title",
    [
        "Postgres 18 released",
        "Why I left San Francisco",
        "A history of the bicycle",
        "Rust's borrow checker explained",
    ],
)
def test_irrelevant_titles_do_not_match(title):
    assert not is_relevant(title)


@pytest.mark.parametrize(
    "title",
    [
        "Thailand raises tariffs",  # contains "ai" inside a word
        "Chairman steps down",  # "ai" inside "Chairman"
        "Repairing an old radio",  # "ai" inside "Repairing"
        "The Ragtime revival",  # "rag" inside "Ragtime"
        "Plaintiffs file suit",  # "ai" inside "Plaintiffs"
    ],
)
def test_substrings_inside_words_do_not_match(title):
    assert not is_relevant(title)


def test_url_is_searched_as_well_as_title():
    assert is_relevant("A neat trick", "https://arxiv.org/abs/2401.00001")


def test_matched_terms_are_deduplicated_and_lowercased():
    terms = matched_terms("LLM benchmarks for LLMs", None)
    assert terms == ["llm", "benchmarks", "llms"]


def test_matched_terms_ignores_none_and_empty():
    assert matched_terms(None, "") == []


@pytest.mark.parametrize(
    "title",
    [
        # Research headlines carrying no vendor or model name. These were
        # silently dropped until a two-run demo surfaced the gap.
        "Interpretability results for sparse autoencoders",
        "New alignment technique for reasoning models",
        "Scaling laws for post-training",
        "Chain-of-thought prompting revisited",
        "Reducing hallucinations with retrieval",
        "A mixture of experts architecture at 400B",
        "Open-weight models catch up on reasoning",
        "Knowledge distillation for smaller models",
        "AGI is closer than you think",
        "Superintelligence and the alignment problem",
    ],
)
def test_research_vocabulary_is_matched(title):
    assert is_relevant(title)


def test_still_ignores_unrelated_headlines_after_widening():
    assert not is_relevant("A history of the bicycle derailleur")
    assert not is_relevant("Postgres 18 adds asynchronous IO")
