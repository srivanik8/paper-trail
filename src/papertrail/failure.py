"""Describing a failure in one line.

Several exception types this project catches carry multi-line messages --
``httpx.HTTPStatusError`` appends a documentation URL on its own line, and
SQLite errors sometimes embed the offending statement. Those strings end up in
run summaries and in GitHub Actions logs, where a message that wraps onto an
unprefixed second line reads as a separate event.

So every recorded failure goes through here: one line, bounded length, type
name first because that is what you scan for.
"""

from __future__ import annotations

#: Longer than this and the tail is never the useful part. The arXiv query URL
#: alone is 130 characters.
MAX_LENGTH = 180


def describe(exc: BaseException, limit: int = MAX_LENGTH) -> str:
    """Render an exception as a single bounded line: ``TypeName: message``."""
    message = " ".join(str(exc).split())
    described = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    if len(described) <= limit:
        return described
    return described[: limit - 1].rstrip() + "…"
