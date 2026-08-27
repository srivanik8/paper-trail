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
