"""Terminal output for a run.

Deliberately dependency-free: a table wide enough to read and narrow enough to
fit an 80-column terminal.
"""

from __future__ import annotations

import shutil

from .models import Item
from .timeutil import isoformat_utc

MIN_TITLE_WIDTH = 24


def _truncate(text: str, width: int) -> str:
    """Clip ``text`` to ``width``, ending in an ellipsis when it does not fit."""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)].rstrip() + "…"


def format_table(items: list[Item], width: int | None = None) -> str:
    """Render items as a fixed-column table: signal, time, source, title."""
    if not items:
        return "no items"

    total = width or shutil.get_terminal_size((100, 24)).columns
    # signal(7) + gap + time(16) + gap + source(6) + gap = 34 columns of chrome.
    title_width = max(MIN_TITLE_WIDTH, total - 34)

    header = f"{'SIGNAL':>7}  {'PUBLISHED (UTC)':<16}  {'SRC':<6}  TITLE"
    lines = [header, "-" * min(total, len(header) + title_width - 5)]

    for item in items:
        published = isoformat_utc(item.published_at)[5:16].replace("T", " ")
        lines.append(
            f"{item.raw_signal:>7.1f}  {published:<16}  {item.source:<6}  "
            f"{_truncate(item.title, title_width)}"
        )
    return "\n".join(lines)


def format_summary(count: int, per_source: dict[str, int], errors: dict[str, str]) -> str:
    """One-line tally of a run, plus a line per failed source."""
    breakdown = ", ".join(f"{name} {n}" for name, n in sorted(per_source.items()))
    line = f"{count} item{'' if count == 1 else 's'}"
    if breakdown:
        line += f" ({breakdown})"
    lines = [line]
    lines.extend(f"  ! {name}: {message}" for name, message in sorted(errors.items()))
    return "\n".join(lines)
