"""Terminal output for a run.

Deliberately dependency-free: a table wide enough to read and narrow enough to
fit an 80-column terminal.
"""

from __future__ import annotations

import shutil

from .pipeline import RunResult, Story
from .provenance import Evidence
from .timeutil import isoformat_utc

MIN_TITLE_WIDTH = 24

#: Four characters each, so the column stays aligned.
EVIDENCE_BADGE: dict[Evidence, str] = {
    Evidence.PAPER: "pape",
    Evidence.REPO: "repo",
    Evidence.MODEL_WEIGHTS: "wgts",
    Evidence.OFFICIAL_BLOG: "blog",
    Evidence.NONE: "  - ",
}


def _truncate(text: str, width: int) -> str:
    """Clip ``text`` to ``width``, ending in an ellipsis when it does not fit."""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)].rstrip() + "…"


def format_table(stories: list[Story], width: int | None = None) -> str:
    """Render stories as a fixed-column table.

    A leading ``~`` marks a story a previous run already reported. The
    ``EVID`` column names what the story can be checked against. A trailing
    ``+src,src`` names the other outlets that carried it.
    """
    if not stories:
        return "no items"

    total = width or shutil.get_terminal_size((100, 24)).columns
    # mark(1) + signal(7) + gap + time(12) + gap + src(6) + gap + evid(4) + gaps.
    title_width = max(MIN_TITLE_WIDTH, total - 40)

    header = f" {'SIGNAL':>7}  {'PUBLISHED':<12}  {'SRC':<6}  {'EVID':<4}  TITLE"
    lines = [header, "-" * min(total, len(header) + title_width - 5)]

    for story in stories:
        item = story.canonical
        published = isoformat_utc(item.published_at)[5:16].replace("T", " ")
        also = f"  +{','.join(story.cluster.also_seen)}" if story.cluster.also_seen else ""
        mark = "~" if story.cluster.is_continuation else " "
        badge = EVIDENCE_BADGE[story.evidence]
        title = _truncate(item.title, title_width - len(also))
        lines.append(
            f"{mark}{item.raw_signal:>7.1f}  {published:<12}  "
            f"{item.source:<6}  {badge:<4}  {title}{also}"
        )
    return "\n".join(lines)


def format_summary(result: RunResult) -> str:
    """Tally of a run: what came in, what survived, and on what evidence."""
    count = len(result.stories)
    lines = [
        f"fetched {result.fetched}"
        f" -> {count} stor{'y' if count == 1 else 'ies'}"
        f" ({len(result.fresh)} new, {len(result.continuing)} seen before,"
        f" {result.collapsed} folded in, {len(result.dropped)} unsourced)"
    ]

    evidence = ", ".join(f"{name} {n}" for name, n in sorted(result.per_evidence.items()))
    if evidence:
        lines.append(f"evidence: {evidence}")
    if result.pages_fetched:
        lines.append(f"pages fetched: {result.pages_fetched}")

    lines.extend(f"  ! {name}: {message}" for name, message in sorted(result.errors.items()))
    return "\n".join(lines)
