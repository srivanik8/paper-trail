from papertrail.extract import MAX_LINKS, extract_links

BASE = "https://blog.example.com/posts/a-model"


def test_absolute_links_are_returned_in_document_order():
    html = """
    <article>
      <a href="https://arxiv.org/abs/2401.00001">the paper</a>
      <a href="https://github.com/owner/repo">the code</a>
    </article>
    """
    assert extract_links(html, BASE) == [
        "https://arxiv.org/abs/2401.00001",
        "https://github.com/owner/repo",
    ]


def test_relative_links_are_resolved_against_the_page():
    html = '<a href="/other/post">elsewhere</a>'
    assert extract_links(html, "https://other.example.com/a") == []


def test_links_back_into_the_pages_own_host_are_dropped():
    """A site linking to itself is not corroboration."""
    html = """
    <a href="https://blog.example.com/archive">our archive</a>
    <a href="/relative/page">also ours</a>
    <a href="https://arxiv.org/abs/2401.00001">the paper</a>
    """
    assert extract_links(html, BASE) == ["https://arxiv.org/abs/2401.00001"]


def test_www_and_bare_host_count_as_the_same_origin():
    html = '<a href="https://www.blog.example.com/archive">ours</a>'
    assert extract_links(html, BASE) == []


def test_navigation_and_footer_links_are_ignored():
    """Otherwise every article resolves to whatever the site links in its chrome."""
    html = """
    <nav><a href="https://github.com/thecompany/website">our repo</a></nav>
    <header><a href="https://arxiv.org/abs/9999.99999">a header link</a></header>
    <article><a href="https://arxiv.org/abs/2401.00001">the paper</a></article>
    <aside><a href="https://github.com/sponsor/thing">sponsor</a></aside>
    <footer><a href="https://github.com/thecompany/careers">careers</a></footer>
    """
    assert extract_links(html, BASE) == ["https://arxiv.org/abs/2401.00001"]


def test_nested_navigation_is_tracked_correctly():
    html = """
    <nav><div><ul><li><a href="https://github.com/a/b">nav link</a></li></ul></div></nav>
    <a href="https://arxiv.org/abs/2401.00001">body link</a>
    """
    assert extract_links(html, BASE) == ["https://arxiv.org/abs/2401.00001"]


def test_duplicate_links_appear_once():
    html = """
    <a href="https://arxiv.org/abs/2401.00001">paper</a>
    <a href="https://arxiv.org/abs/2401.00001">the paper again</a>
    """
    assert extract_links(html, BASE) == ["https://arxiv.org/abs/2401.00001"]


def test_non_navigable_schemes_are_skipped():
    html = """
    <a href="mailto:hi@example.com">mail</a>
    <a href="javascript:void(0)">click</a>
    <a href="#section">jump</a>
    <a href="tel:+1234">call</a>
    <a href="https://arxiv.org/abs/2401.00001">paper</a>
    """
    assert extract_links(html, BASE) == ["https://arxiv.org/abs/2401.00001"]


def test_anchors_without_an_href_are_skipped():
    assert extract_links('<a name="top"></a><a href="https://github.com/a/b">x</a>', BASE) == [
        "https://github.com/a/b"
    ]


def test_malformed_markup_does_not_raise():
    html = '<a href="https://arxiv.org/abs/2401.00001">unclosed <div><p>chaos'
    assert extract_links(html, BASE) == ["https://arxiv.org/abs/2401.00001"]


def test_an_empty_page_yields_nothing():
    assert extract_links("", BASE) == []
    assert extract_links("   ", BASE) == []


def test_link_collection_is_capped():
    html = "".join(f'<a href="https://other{i}.example/x">{i}</a>' for i in range(MAX_LINKS + 50))
    assert len(extract_links(html, BASE)) <= MAX_LINKS
