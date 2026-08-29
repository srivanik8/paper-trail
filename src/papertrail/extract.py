"""Pulling candidate links out of a fetched page.

Uses the standard library's HTML parser rather than a dependency: all this
needs is ``href`` attributes and their surrounding anchor text, and real-world
markup is malformed often enough that a lenient parser is the right tool.

Order is preserved and matters. When a page offers two papers, the one that
appears first is the one the article is about; the second is usually a
reference in a footer.
"""

from __future__ import annotations

import contextlib
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

#: Sections of a page whose links are navigation or boilerplate, never the
#: subject. Skipping them is what stops every article resolving to whatever
#: repository the site happens to link in its footer.
_IGNORED_CONTAINERS = frozenset({"nav", "header", "footer", "aside"})

#: Never treated as a candidate, however they are linked.
_IGNORED_SCHEMES = ("mailto:", "javascript:", "tel:", "data:", "#")

MAX_LINKS = 400


class _LinkParser(HTMLParser):
    """Collects hrefs outside navigation containers, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _IGNORED_CONTAINERS:
            self._depth += 1
            return
        if tag != "a" or self._depth or len(self.links) >= MAX_LINKS:
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value.strip())
                break

    def handle_endtag(self, tag: str) -> None:
        if tag in _IGNORED_CONTAINERS and self._depth:
            self._depth -= 1


def extract_links(html: str, base_url: str) -> list[str]:
    """Return absolute, deduplicated outbound links from ``html``.

    Relative hrefs are resolved against ``base_url``. Links back into the
    page's own host are dropped: a site linking to itself is not corroboration,
    and keeping them would let any blog "resolve" to its own archive.
    """
    if not html.strip():
        return []

    parser = _LinkParser()
    # Malformed markup is normal on the open web; take what parsed and move on.
    with contextlib.suppress(Exception):
        parser.feed(html)

    origin = urlsplit(base_url).netloc.lower().removeprefix("www.")
    seen: dict[str, None] = {}

    for href in parser.links:
        if not href or href.startswith(_IGNORED_SCHEMES):
            continue

        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue

        host = urlsplit(absolute).netloc.lower().removeprefix("www.")
        if host == origin:
            continue

        seen.setdefault(absolute, None)

    return list(seen)
