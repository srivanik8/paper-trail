"""Ingesters. Each one turns a remote API into :class:`papertrail.models.Item`."""

from .arxiv import ArxivListings
from .base import Source
from .hn import HackerNews
from .huggingface import HuggingFacePapers

#: Every source the CLI knows how to run, keyed by its ``name``.
REGISTRY: dict[str, type[Source]] = {
    HackerNews.name: HackerNews,
    HuggingFacePapers.name: HuggingFacePapers,
    ArxivListings.name: ArxivListings,
}

__all__ = ["REGISTRY", "ArxivListings", "HackerNews", "HuggingFacePapers", "Source"]
