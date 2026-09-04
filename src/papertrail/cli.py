"""Command line entry point.

papertrail run --since 24h --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from .archive import export as export_archive
from .archive import restore as restore_archive
from .audit import (
    DEFAULT_CASES,
    DEFAULT_REPO_CASES,
    audit,
    audit_repos,
    format_repo_report,
    format_report,
    load_cases,
    load_repo_cases,
)
from .checker import Checker
from .dedup import DEFAULT_THRESHOLD
from .digest import DEFAULT_LIMIT, DEFAULT_MIN_SCORE, build
from .fetcher import Fetcher
from .github import GitHub
from .mailer import Mailer, MailerNotConfigured
from .papers import ArxivPapers
from .pipeline import DEFAULT_DEDUP_WINDOW, build_sources, run
from .render import format_summary, format_table
from .resolver import Resolver
from .scorer import DEFAULT_MODEL, Scorer
from .sources import REGISTRY
from .stats import collect, format_stats, window_from_days
from .store import Store
from .timeutil import isoformat_utc, parse_since, utcnow

DEFAULT_DB = "papertrail.db"
DEFAULT_DATA = "data"


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
        "--no-score",
        action="store_true",
        help="skip LLM scoring; rank by popularity instead",
    )
    run_cmd.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"model used for scoring (default: {DEFAULT_MODEL})",
    )
    run_cmd.add_argument(
        "--rescore",
        action="store_true",
        help="score again even for stories already scored, and pay for it again",
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

    digest_cmd = subcommands.add_parser(
        "digest", help="render the day's digest, and optionally send it"
    )
    digest_cmd.add_argument("--since", default="24h", help="lookback window (default: 24h)")
    digest_cmd.add_argument("--db", default=DEFAULT_DB, help=f"database (default: {DEFAULT_DB})")
    digest_cmd.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"most stories to include (default: {DEFAULT_LIMIT})",
    )
    digest_cmd.add_argument(
        "--min-score",
        type=int,
        default=DEFAULT_MIN_SCORE,
        help=f"lowest score worth including (default: {DEFAULT_MIN_SCORE})",
    )
    digest_cmd.add_argument("--model", default=DEFAULT_MODEL, help="model used for scoring")
    digest_cmd.add_argument("--no-score", action="store_true", help="skip scoring")
    digest_cmd.add_argument("--no-fetch", action="store_true", help="never read a page")
    digest_cmd.add_argument("--no-check", action="store_true", help="skip the reality checks")
    digest_cmd.add_argument(
        "--out",
        default="out/digest.html",
        help="where to write the rendered HTML (default: out/digest.html)",
    )
    digest_cmd.add_argument("--send", action="store_true", help="email the digest")
    digest_cmd.add_argument("--to", default=None, help="recipient (default: $PAPERTRAIL_TO)")
    digest_cmd.add_argument(
        "--again",
        action="store_true",
        help="send even if today's digest already went out",
    )
    digest_cmd.add_argument(
        "--empty-ok",
        action="store_true",
        help="send even when nothing cleared the bar",
    )
    digest_cmd.add_argument(
        "--data",
        default=None,
        help="also write the JSONL archive here after the run",
    )

    export_cmd = subcommands.add_parser("export", help="write the store to JSONL for committing")
    export_cmd.add_argument("--db", default=DEFAULT_DB, help=f"database (default: {DEFAULT_DB})")
    export_cmd.add_argument(
        "--data", default=DEFAULT_DATA, help=f"archive directory (default: {DEFAULT_DATA})"
    )

    restore_cmd = subcommands.add_parser("restore", help="rebuild the store from committed JSONL")
    restore_cmd.add_argument("--db", default=DEFAULT_DB, help=f"database (default: {DEFAULT_DB})")
    restore_cmd.add_argument(
        "--data", default=DEFAULT_DATA, help=f"archive directory (default: {DEFAULT_DATA})"
    )

    stats_cmd = subcommands.add_parser("stats", help="summarize what the filter has decided so far")
    stats_cmd.add_argument("--db", default=DEFAULT_DB, help=f"database (default: {DEFAULT_DB})")
    stats_cmd.add_argument(
        "--days",
        type=int,
        default=0,
        help="limit to items first seen in the last N days (default: all time)",
    )

    audit_cmd = subcommands.add_parser(
        "audit", help="score the classifier and the substance rules against hand labels"
    )
    audit_cmd.add_argument(
        "--kind",
        choices=("provenance", "repos", "all"),
        default="all",
        help="which rules to score (default: all)",
    )
    audit_cmd.add_argument(
        "--cases",
        default=str(DEFAULT_CASES),
        help="JSONL file of {url, expected, note} records",
    )
    audit_cmd.add_argument(
        "--repo-cases",
        default=str(DEFAULT_REPO_CASES),
        help="JSONL file of {slug, verdict, facts, note} records",
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

    if args.command == "digest":
        return _digest(args)

    if args.command == "stats":
        with Store(args.db) as store:
            print(format_stats(collect(store, window_from_days(args.days))))
        return 0

    if args.command == "export":
        with Store(args.db) as store:
            counts = export_archive(store, args.data)
        print(f"exported {counts.items} items, {counts.scores} scores to {args.data}/")
        return 0

    if args.command == "restore":
        with Store(args.db) as store:
            counts = restore_archive(store, args.data)
        print(f"restored {counts.items} items, {counts.scores} scores from {args.data}/")
        return 0

    try:
        window = parse_since(args.since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sources = build_sources(args.source, min_points=args.min_points)

    with Store(args.db) as store:
        resolver = Resolver(None if args.no_fetch else Fetcher(store))
        checker = None if args.no_check else Checker(GitHub(store), ArxivPapers(store))
        scorer = None if args.no_score else Scorer(store, model=args.model, reuse=not args.rescore)
        result = run(
            window,
            sources,
            store,
            resolver=resolver,
            checker=checker,
            scorer=scorer,
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
            if story.score is not None:
                payload["signal_score"] = story.score.signal_score
                payload["category"] = story.score.category.value
                payload["one_line"] = story.score.one_line
                payload["hype_flags"] = [flag.value for flag in story.score.hype_flags]
                payload["why"] = story.score.why
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


def _digest(args: argparse.Namespace) -> int:
    """Render the day's digest, write it to disk, and optionally send it."""
    try:
        window = parse_since(args.since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    today = utcnow().strftime("%Y-%m-%d")
    sources = build_sources(None)

    with Store(args.db) as store:
        result = run(
            window,
            sources,
            store,
            resolver=Resolver(None if args.no_fetch else Fetcher(store)),
            checker=None if args.no_check else Checker(GitHub(store), ArxivPapers(store)),
            scorer=None if args.no_score else Scorer(store, model=args.model),
        )

        # Anything already sent today is not news, however well it scored.
        fresh = [
            story
            for story in result.stories
            if args.again or not store.was_sent(story.canonical.id)
        ]
        digest = build(
            fresh,
            limit=args.limit,
            min_score=args.min_score,
            dropped=len(result.dropped),
        )

        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(digest.html, encoding="utf-8")

        print(format_summary(result))
        print(f"digest: {len(digest.stories)} stor{'y' if len(digest.stories) == 1 else 'ies'}")
        print(f"subject: {digest.subject}")
        print(f"written: {path}")

        if not args.send:
            _archive(store, args.data)
            return 0

        if digest.empty and not args.empty_ok:
            print("not sending: nothing cleared the bar (use --empty-ok to send anyway)")
            _archive(store, args.data)
            return 0

        mailer = Mailer(recipient=args.to)
        try:
            delivery = mailer.send(digest)
        except MailerNotConfigured as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        if not delivery.ok:
            print(f"error: send failed: {delivery.error}", file=sys.stderr)
            return 1

        store.mark_sent([story.canonical.id for story in digest.stories], today)
        print(
            f"sent to {mailer.recipient}"
            + (f" ({delivery.message_id})" if delivery.message_id else "")
        )
        return 0


def _archive(store: Store, directory: str | None) -> None:
    """Write the JSONL archive, if the caller asked for one.

    Called on every exit path that got far enough to change the store, so a
    scheduled run always commits what it learned -- including on a day when
    nothing was worth sending.
    """
    if directory is None:
        return
    counts = export_archive(store, directory)
    print(f"archived {counts.items} items, {counts.scores} scores to {directory}/")


def _audit(args: argparse.Namespace) -> int:
    """Score the requested rule sets and print the results."""
    accuracies = []
    try:
        if args.kind in ("provenance", "all"):
            report = audit(load_cases(args.cases))
            print(format_report(report))
            accuracies.append(report.accuracy)

        if args.kind in ("repos", "all"):
            if accuracies:
                print()
            repo_report = audit_repos(load_repo_cases(args.repo_cases), utcnow())
            print(format_repo_report(repo_report))
            accuracies.append(repo_report.accuracy)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return 0 if min(accuracies) >= args.min_accuracy else 1


if __name__ == "__main__":
    raise SystemExit(main())
