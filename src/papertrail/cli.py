"""Command line entry point.

papertrail run --since 24h --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys

from .pipeline import build_sources, run
from .render import format_summary, format_table
from .sources import REGISTRY
from .timeutil import isoformat_utc, parse_since


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="papertrail",
        description="AI news, filtered by whether it can show its work.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run_cmd = subcommands.add_parser("run", help="fetch and rank recent items")
    run_cmd.add_argument(
        "--since",
        default="24h",
        help="lookback window, e.g. 24h, 90m, 7d (default: 24h)",
    )
    run_cmd.add_argument(
        "--source",
        action="append",
        choices=sorted(REGISTRY),
        help="restrict to one source; repeatable (default: all)",
    )
    run_cmd.add_argument(
        "--min-points",
        type=int,
        default=5,
        help="drop items below this source-native score (default: 5)",
    )
    run_cmd.add_argument(
        "--limit",
        type=int,
        default=0,
        help="show at most this many items; 0 means all (default: 0)",
    )
    run_cmd.add_argument(
        "--json",
        action="store_true",
        help="emit newline-delimited JSON instead of a table",
    )
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="do not persist anything (day 1: nothing persists either way)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    try:
        window = parse_since(args.since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sources = build_sources(args.source, min_points=args.min_points)
    result = run(window, sources)

    items = result.items[: args.limit] if args.limit > 0 else result.items

    if args.json:
        for item in items:
            print(json.dumps(item.to_dict(), ensure_ascii=False))
    else:
        print(f"window: since {isoformat_utc(result.since)}")
        print(format_summary(len(result.items), result.per_source, result.errors))
        print()
        print(format_table(items))

    # A run where every source failed is a failed run.
    if result.errors and not result.items:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
