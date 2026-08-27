"""Ingesters. Each one turns a remote API into :class:`papertrail.models.Item`."""

from .base import Source
from .hn import HackerNews

#: Every source the CLI knows how to run, keyed by its ``name``.
REGISTRY: dict[str, type[Source]] = {
    HackerNews.name: HackerNews,
}

__all__ = ["REGISTRY", "HackerNews", "Source"]
