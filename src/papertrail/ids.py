"""Stable identity for an item.

The id is derived from the item's URL and nothing else, so the same story
arriving from two ingesters collapses to one row. Everything that decides what
"the same URL" means lives in :func:`canonical_url` -- widening the rules is a
change to one function, never to a call site.

Canonicalization is the cheap half of deduplication. It catches the same link
wearing different tracking params; it cannot catch the same launch written up
under two different headlines on two different domains. That is what the fuzzy
title pass in :mod:`papertrail.dedup` is for.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ID_LENGTH = 16

#: Query parameters that identify a referrer or campaign rather than content.
#: Anything prefixed ``utm_`` is dropped too -- see :func:`_strip_tracking`.
_TRACKING_PARAMS = frozenset(
    {
        "__twitter_impression",
        "_hsenc",
        "_hsmi",
        "at_campaign",
        "at_medium",
        "campaign_id",
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "ref",
        "ref_src",
        "ref_url",
        "referer",
        "referrer",
        "spm",
        "trk",
        "vero_id",
        "wt_mc",
        "yclid",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}

# arXiv exposes one paper at several URLs: /abs/, /pdf/, with or without a
# version suffix. They are the same paper and must share an id.
_ARXIV_HOSTS = frozenset({"arxiv.org", "export.arxiv.org", "static.arxiv.org"})

#: Hosts that serve identical content under more than one name.
_HOST_ALIASES = {host: "arxiv.org" for host in _ARXIV_HOSTS}
_ARXIV_PATH = re.compile(r"^/(?:abs|pdf|html)/(?P<id>.+?)(?:v\d+)?(?:\.pdf)?$", re.IGNORECASE)


def _strip_tracking(query: str) -> str:
    """Drop tracking parameters and sort what remains.

    Sorting means ``?b=2&a=1`` and ``?a=1&b=2`` reach the same id; parameter
    order carries no meaning for any source we ingest.
    """
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlencode(sorted(kept))


def _normalize_host(netloc: str, scheme: str) -> str:
    """Lowercase the host, drop credentials, ``www.`` and the default port."""
    host = netloc.rsplit("@", 1)[-1].lower()

    port = ""
    if ":" in host and not host.endswith("]"):
        host, _, port = host.rpartition(":")
    if port == _DEFAULT_PORTS.get(scheme):
        port = ""

    if host.startswith("www."):
        host = host[4:]

    host = _HOST_ALIASES.get(host, host)

    return f"{host}:{port}" if port else host


def _normalize_path(host: str, path: str) -> str:
    """Apply per-site path rules, then drop a trailing slash."""
    if host in _ARXIV_HOSTS:
        match = _ARXIV_PATH.match(path)
        if match is not None:
            return f"/abs/{match.group('id')}"

    if host == "github.com" and path.lower().endswith(".git"):
        path = path[: -len(".git")]

    # "/" is meaningful; "/path/" and "/path" are not distinct anywhere we read.
    return path.rstrip("/") if path != "/" else path


def canonical_url(url: str) -> str:
    """Reduce a URL to the form used for identity.

    Applies, in order: whitespace trim, scheme normalization to ``https``,
    host lowercasing (minus credentials, ``www.`` and default ports), per-site
    path rules, tracking-parameter removal with the remainder sorted, and
    fragment removal.

    A URL that is not http(s) -- ``mailto:``, a bare fragment -- is returned
    trimmed and otherwise untouched, since none of these rules apply to it.
    """
    text = url.strip()
    if not text:
        return text

    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        return text

    # http and https serve the same story; the scheme is not part of identity.
    host = _normalize_host(parts.netloc, parts.scheme.lower())
    return urlunsplit(
        (
            "https",
            host,
            _normalize_path(host, parts.path),
            _strip_tracking(parts.query),
            "",  # fragments address a position on a page, not a distinct page
        )
    )


def item_id(url: str) -> str:
    """Return the stable id for ``url``: a truncated SHA-256 of its canonical form."""
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()
    return digest[:ID_LENGTH]
