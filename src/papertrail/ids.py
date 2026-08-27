"""Stable identity for an item.

The id is derived from the item's URL and nothing else, so the same story
arriving from two ingesters collapses to one row. URL canonicalization lives
inside :func:`canonical_url` -- it is deliberately the only place that decides
what "the same URL" means, so widening it later changes one function rather
than every call site.
"""

from __future__ import annotations

import hashlib

ID_LENGTH = 16


def canonical_url(url: str) -> str:
    """Reduce a URL to the form used for identity.

    Day 1 does the cheap, always-safe part: trim surrounding whitespace. Query
    stripping, host lowercasing and redirect unwrapping arrive on day 2, when
    there is a store to dedupe against.
    """
    return url.strip()


def item_id(url: str) -> str:
    """Return the stable id for ``url``: a truncated SHA-256 of its canonical form."""
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()
    return digest[:ID_LENGTH]
