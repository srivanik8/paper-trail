"""Terminal output for a run.

Deliberately dependency-free: a table wide enough to read and narrow enough to
fit an 80-column terminal.
"""

from __future__ import annotations

import shutil

from .dedup import Cluster
from .pipeline import RunResult
from .timeutil import isoformat_utc

MIN_TITLE_WIDTH = 24


def _truncate(text: str, width: int) -> str:
    """Clip ``text`` to ``width``, ending in an ellipsis when it does not fit."""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)].rstrip() + "…"


def format_table(clusters: list[Cluster], width: int | None = None) -> str:
    """Render clusters as a fixed-column table: signal, time, source, title.

    A leading ``~`` marks a story a previous run already reported. A trailing
    ``+src,src`` names the other outlets that carried it.
    """
    if not clusters:
        return "no items"

    total = width or shutil.get_terminal_size((100, 24)).columns
    # mark(1) + signal(7) + gap + time(16) + gap + source(6) + gap = 35 columns.
    title_width = max(MIN_TITLE_WIDTH, total - 35)

    header = f" {'SIGNAL':>7}  {'PUBLISHED (UTC)':<16}  {'SRC':<6}  TITLE"
    lines = [header, "-" * min(total, len(header) + title_width - 5)]

    for cluster in clusters:
        item = cluster.canonical
        published = isoformat_utc(item.published_at)[5:16].replace("T", " ")
        also = f"  +{','.join(cluster.also_seen)}" if cluster.also_seen else ""
        mark = "~" if cluster.is_continuation else " "
        title = _truncate(item.title, title_width - len(also))
        lines.append(
            f"{mark}{item.raw_signal:>7.1f}  {published:<16}  {item.source:<6}  {title}{also}"
        )
    return "\n".join(lines)


def format_summary(result: RunResult) -> str:
    """Tally of a run: what was fetched, what collapsed, what is actually new."""
    breakdown = ", ".join(f"{name} {n}" for name, n in sorted(result.per_source.items()))
    lines = [
        f"fetched {result.fetched}"
        f" -> {len(result.clusters)} stor{'y' if len(result.clusters) == 1 else 'ies'}"
        f" ({len(result.fresh)} new, {len(result.continuing)} seen before,"
        f" {result.collapsed} folded in)" + (f" [{breakdown}]" if breakdown else "")
    ]
    lines.extend(f"  ! {name}: {message}" for name, message in sorted(result.errors.items()))
    return "\n".join(lines)
