import pytest

from papertrail.provenance import (
    NONE,
    Evidence,
    Provenance,
    best,
    classify,
    repo_slug,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/abs/2401.00001",
        "https://arxiv.org/pdf/2401.00001v2.pdf",  # canonicalized to /abs/ first
        "https://openreview.net/forum?id=abc",
        "https://aclanthology.org/2024.acl-long.1",
        "https://proceedings.mlr.press/v202/someone23a.html",
        "https://dl.acm.org/doi/10.1145/1234567",
        "https://doi.org/10.1038/s41586-024-00001-0",
    ],
)
def test_papers_are_recognized(url):
    assert classify(url).evidence is Evidence.PAPER


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo/blob/main/README.md",
        "https://github.com/owner/repo/releases/tag/v1.0",
        "https://gitlab.com/owner/repo",
        "https://codeberg.org/owner/repo",
    ],
)
def test_repositories_are_recognized(url):
    assert classify(url).evidence is Evidence.REPO


def test_a_deep_repo_link_resolves_to_the_repository_root():
    result = classify("https://github.com/owner/repo/blob/main/src/train.py")
    assert result.url == "https://github.com/owner/repo"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/trending",
        "https://github.com/features/copilot",
        "https://github.com/pricing",
        "https://github.com/owner",  # an account, not a repository
        "https://github.com/",
    ],
)
def test_github_site_pages_are_not_repositories(url):
    assert classify(url).evidence is Evidence.NONE
    assert repo_slug(url) is None


def test_repo_slug_extracts_owner_and_name():
    assert repo_slug("https://github.com/ggerganov/llama.cpp/tree/master") == "ggerganov/llama.cpp"


@pytest.mark.parametrize(
    "url",
    [
        "https://huggingface.co/meta-llama/Llama-3-8B",
        "https://huggingface.co/mistralai/Mistral-7B-v0.1",
        "https://huggingface.co/datasets/squad",
    ],
)
def test_published_weights_are_recognized(url):
    assert classify(url).evidence is Evidence.MODEL_WEIGHTS


def test_a_hugging_face_paper_page_resolves_to_the_paper_itself():
    result = classify("https://huggingface.co/papers/2401.00001")
    assert result.evidence is Evidence.PAPER
    assert result.url == "https://arxiv.org/abs/2401.00001"


@pytest.mark.parametrize(
    "url",
    ["https://huggingface.co/blog/some-post", "https://huggingface.co/pricing"],
)
def test_hugging_face_site_pages_are_not_weights(url):
    assert classify(url).evidence is not Evidence.MODEL_WEIGHTS


@pytest.mark.parametrize(
    "url",
    [
        "https://www.anthropic.com/news/some-announcement",
        "https://openai.com/index/some-post",
        "https://deepmind.google/discover/blog/a-result",
        "https://ai.meta.com/blog/a-release",
        "https://mistral.ai/news/a-model",
        "https://blog.google/technology/ai/a-post",
        "https://www.databricks.com/blog/a-post",
    ],
)
def test_official_lab_posts_are_recognized(url):
    assert classify(url).evidence is Evidence.OFFICIAL_BLOG


@pytest.mark.parametrize(
    "url",
    [
        # A general-purpose domain outside its research section.
        "https://blog.google/products/pixel/a-phone",
        "https://www.databricks.com/company/careers",
    ],
)
def test_lab_domains_outside_their_research_sections_are_not_evidence(url):
    assert classify(url).evidence is Evidence.NONE


@pytest.mark.parametrize(
    "url",
    [
        "https://techcrunch.com/2026/01/01/a-startup-raised-money",
        "https://medium.com/@someone/why-agi-is-near",
        "https://example.com/waitlist",
        "https://twitter.com/someone/status/1",
        "https://news.ycombinator.com/item?id=1",
        "",
        "   ",
        "not a url at all",
    ],
)
def test_everything_else_is_not_evidence(url):
    assert classify(url).evidence is Evidence.NONE
    assert classify(url).url is None


def test_none_reports_itself_as_unresolved():
    assert NONE.resolved is False
    assert classify("https://arxiv.org/abs/2401.00001").resolved is True


def test_via_is_carried_through():
    assert classify("https://arxiv.org/abs/2401.00001", via="page").via == "page"


def test_classification_canonicalizes_the_url_it_returns():
    result = classify("http://WWW.arxiv.org/pdf/2401.00001v3?utm_source=x")
    assert result.url == "https://arxiv.org/abs/2401.00001"


def test_best_prefers_a_paper_over_a_blog_post():
    chosen = best(
        [
            classify("https://www.anthropic.com/news/a-post"),
            classify("https://arxiv.org/abs/2401.00001"),
            classify("https://github.com/owner/repo"),
        ]
    )
    assert chosen.evidence is Evidence.PAPER


def test_best_prefers_a_repo_over_weights():
    chosen = best(
        [
            classify("https://huggingface.co/org/model"),
            classify("https://github.com/owner/repo"),
        ]
    )
    assert chosen.evidence is Evidence.REPO


def test_best_of_nothing_is_none():
    assert best([]) is NONE
    assert best([classify("https://example.com/x")]).evidence is Evidence.NONE


def test_best_keeps_the_first_of_equally_strong_candidates():
    chosen = best(
        [
            classify("https://arxiv.org/abs/2401.00001"),
            classify("https://arxiv.org/abs/2401.00002"),
        ]
    )
    assert chosen.url == "https://arxiv.org/abs/2401.00001"


def test_a_lab_blog_is_the_weakest_evidence():
    """The lab is the interested party: a post proves an announcement, not a result."""
    blog = classify("https://openai.com/index/a-post")
    for stronger in (
        "https://arxiv.org/abs/2401.1",
        "https://github.com/a/b",
        "https://huggingface.co/a/b",
    ):
        assert classify(stronger).strength > blog.strength


def test_provenance_is_immutable():
    result = classify("https://arxiv.org/abs/2401.00001")
    with pytest.raises(AttributeError):
        result.url = "https://example.com"  # type: ignore[misc]


def test_subdomains_of_a_lab_are_recognized():
    assert classify("https://www.anthropic.com/news/x").evidence is Evidence.OFFICIAL_BLOG


def test_provenance_can_be_constructed_directly():
    assert Provenance(Evidence.PAPER, "https://arxiv.org/abs/1").resolved is True
