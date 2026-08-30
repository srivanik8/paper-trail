"""Command line entry point.

papertrail run --since 24h --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta

from .audit import DEFAULT_CASES, audit, format_report, load_cases
from .checker import Checker
from .dedup import DEFAULT_THRESHOLD
from .fetcher import Fetcher
from .github import GitHub
from .papers import ArxivPapers
from .pipeline import DEFAULT_DEDUP_WINDOW, build_sources, run
from .render import format_summary, format_table
from .resolver import Resolver
from .sources import REGISTRY
from .store import Store
from .timeutil import isoformat_utc, parse_since

DEFAULT_DB = "papertrail.db"


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
        "--db",
        default=DEFAULT_DB,
        help=f"SQLite database recording what has been seen (default: {DEFAULT_DB})",
    )
    run_cmd.add_argument(
        "--dedup-days",
        type=int,
        default=DEFAULT_DEDUP_WINDOW.days,
        help=(
            "how far back to look for stories already handled "
            f"(default: {DEFAULT_DEDUP_WINDOW.days})"
        ),
    )
    run_cmd.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"title similarity required to merge two items (default: {DEFAULT_THRESHOLD})",
    )
    run_cmd.add_argument(
        "--new-only",
        action="store_true",
        help="show only stories no previous run reported",
    )
    run_cmd.add_argument(
        "--no-fetch",
        action="store_true",
        help="resolve from URLs alone; never read a page",
    )
    run_cmd.add_argument(
        "--no-check",
        action="store_true",
        help="skip the repository and paper reality checks",
    )
    run_cmd.add_argument(
        "--keep-unsourced",
        action="store_true",
        help="keep stories with no primary source, to see what the resolver misses",
    )
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="read the store to deduplicate, but record nothing",
    )

    audit_cmd = subcommands.add_parser(
        "audit", help="score the provenance classifier against hand-labelled cases"
    )
    audit_cmd.add_argument(
        "--cases",
        default=str(DEFAULT_CASES),
        help="JSONL file of {url, expected, note} records",
    )
    audit_cmd.add_argument(
        "--min-accuracy",
        type=float,
        default=0.0,
        help="exit non-zero below this accuracy, for use in CI (default: 0)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    if args.command == "audit":
        return _audit(args)

    try:
        window = parse_since(args.since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sources = build_sources(args.source, min_points=args.min_points)

    with Store(args.db) as store:
        resolver = Resolver(None if args.no_fetch else Fetcher(store))
        checker = None if args.no_check else Checker(GitHub(store), ArxivPapers(store))
        result = run(
            window,
            sources,
            store,
            resolver=resolver,
            checker=checker,
            dedup_window=timedelta(days=args.dedup_days),
            threshold=args.threshold,
            persist=not args.dry_run,
            require_provenance=not args.keep_unsourced,
        )

    stories = result.fresh if args.new_only else result.stories
    if args.limit > 0:
        stories = stories[: args.limit]

    if args.json:
        for story in stories:
            payload = story.canonical.to_dict()
            payload["cluster_id"] = story.cluster.cluster_id
            payload["also_seen"] = story.cluster.also_seen
            payload["evidence"] = story.evidence.value
            payload["primary_source_url"] = story.provenance.url
            payload["provenance_via"] = story.provenance.via
            payload["substance_flags"] = [flag.value for flag in story.substance.flags]
            payload["star_velocity"] = story.substance.star_velocity
            payload["thin"] = story.thin
            payload["seen_before"] = story.cluster.is_continuation
            print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"window: since {isoformat_utc(result.since)}")
        print(format_summary(result))
        print()
        print(format_table(stories))

    # A run where every source failed is a failed run.
    if result.errors and not result.stories:
        return 1
    return 0


def _audit(args: argparse.Namespace) -> int:
    """Run the classifier over a case file and print the score."""
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = audit(cases)
    print(format_report(report))
    return 0 if report.accuracy >= args.min_accuracy else 1


if __name__ == "__main__":
    raise SystemExit(main())
