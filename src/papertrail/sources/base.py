"""The contract every ingester implements."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from ..models import Item


@runtime_checkable
class Source(Protocol):
    """A remote feed that can be asked for items published since a moment.

    Implementations are responsible for their own pagination and rate limiting,
    and must return items already normalized to UTC. They should not deduplicate
    or persist -- that is the pipeline's job, not the source's.
    """

    #: Short stable identifier, stored on every item this source produces.
    name: str

    def fetch(self, since: datetime) -> list[Item]:
        """Return items published at or after ``since`` (an aware UTC datetime)."""
        ...
