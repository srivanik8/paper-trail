import pytest

from papertrail.ids import ID_LENGTH, canonical_url, item_id


def same(a: str, b: str) -> bool:
    return canonical_url(a) == canonical_url(b)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  https://example.com/post  ", "https://example.com/post"),
        ("HTTPS://Example.COM/post", "https://example.com/post"),
        ("http://example.com/post", "https://example.com/post"),
        ("https://www.example.com/post", "https://example.com/post"),
        ("https://example.com:443/post", "https://example.com/post"),
        ("http://example.com:80/post", "https://example.com/post"),
        ("https://example.com/post/", "https://example.com/post"),
        ("https://example.com/post#section-2", "https://example.com/post"),
        ("https://user:pw@example.com/post", "https://example.com/post"),
    ],
)
def test_normalizes_scheme_host_and_path(raw, expected):
    assert canonical_url(raw) == expected


def test_bare_root_keeps_its_slash():
    assert canonical_url("https://example.com/") == "https://example.com/"


@pytest.mark.parametrize(
    "tracked",
    [
        "https://example.com/post?utm_source=newsletter&utm_medium=email",
        "https://example.com/post?fbclid=abc123",
        "https://example.com/post?ref=hackernews",
        "https://example.com/post?gclid=x&mc_cid=y&igshid=z",
        "https://example.com/post?utm_campaign=launch&ref_src=twsrc",
    ],
)
def test_tracking_parameters_are_stripped(tracked):
    assert canonical_url(tracked) == "https://example.com/post"


def test_meaningful_parameters_survive():
    assert (
        canonical_url("https://example.com/p?id=42&utm_source=x") == "https://example.com/p?id=42"
    )


def test_parameter_order_does_not_change_identity():
    assert same("https://example.com/p?a=1&b=2", "https://example.com/p?b=2&a=1")


def test_blank_valued_parameters_are_kept():
    assert canonical_url("https://example.com/p?flag=") == "https://example.com/p?flag="


@pytest.mark.parametrize(
    "variant",
    [
        "https://arxiv.org/abs/2401.00001",
        "https://arxiv.org/abs/2401.00001v2",
        "https://arxiv.org/pdf/2401.00001",
        "https://arxiv.org/pdf/2401.00001v3",
        "https://arxiv.org/pdf/2401.00001v3.pdf",
        "https://arxiv.org/html/2401.00001v1",
        "http://www.arxiv.org/abs/2401.00001",
        "https://export.arxiv.org/abs/2401.00001",
    ],
)
def test_every_arxiv_url_for_one_paper_collapses(variant):
    assert canonical_url(variant) == "https://arxiv.org/abs/2401.00001"


def test_distinct_arxiv_papers_stay_distinct():
    assert not same("https://arxiv.org/abs/2401.00001", "https://arxiv.org/abs/2401.00002")


def test_github_dot_git_suffix_is_dropped():
    assert same("https://github.com/owner/repo.git", "https://github.com/owner/repo")


def test_non_http_urls_pass_through_untouched():
    assert canonical_url("mailto:someone@example.com") == "mailto:someone@example.com"
    assert canonical_url("") == ""


def test_different_paths_are_never_conflated():
    assert not same("https://example.com/a", "https://example.com/b")
    assert not same("https://example.com/p?id=1", "https://example.com/p?id=2")


def test_item_id_shape_and_stability():
    first = item_id("https://example.com/post")
    assert len(first) == ID_LENGTH
    assert first == item_id("  http://WWW.example.com/post/?utm_source=x#frag  ")
