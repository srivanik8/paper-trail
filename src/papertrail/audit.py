"""Scoring the classifier against hand-labelled cases.

Both acceptance criteria in this project are numbers, not feelings: state the
classifier's precision, and show that the substance rules separate real
projects from launch pages. This module produces both, and re-produces them on
every commit.

**What it does and does not measure.** The cases in ``data/provenance_cases.jsonl``
are hand-labelled URL *shapes*, and those in ``data/repo_cases.jsonl`` are
hand-authored fact profiles in the shape the GitHub API returns. Scoring against
them tells you whether the rules still agree with a human's judgement -- a
regression guard and a calibration check. Neither is a field measurement of live
pages or live repositories; getting that means pointing the resolver and the
checker at a day of real traffic and labelling what comes back. Extend the files
when you do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .provenance import Evidence, classify
from .substance import RepoFacts, Substance, assess_repo
from .timeutil import parse_iso

_DATA = Path(__file__).resolve().parents[2] / "data"
DEFAULT_CASES = _DATA / "provenance_cases.jsonl"
DEFAULT_REPO_CASES = _DATA / "repo_cases.jsonl"


@dataclass(frozen=True, slots=True)
class Case:
    """One hand-labelled URL."""

    url: str
    expected: Evidence
    note: str = ""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the classifier said about a case."""

    case: Case
    actual: Evidence

    @property
    def correct(self) -> bool:
        """True if the classifier agreed with the label."""
        return self.actual is self.case.expected


@dataclass(frozen=True, slots=True)
class Report:
    """Aggregate scoring over a case set."""

    outcomes: list[Outcome]

    @property
    def total(self) -> int:
        """How many cases were scored."""
        return len(self.outcomes)

    @property
    def correct(self) -> int:
        """How many the classifier got right."""
        return sum(1 for outcome in self.outcomes if outcome.correct)

    @property
    def accuracy(self) -> float:
        """Fraction correct across every class, 0-1."""
        return self.correct / self.total if self.total else 0.0

    @property
    def mistakes(self) -> list[Outcome]:
        """Every case the classifier got wrong."""
        return [outcome for outcome in self.outcomes if not outcome.correct]

    def precision(self, evidence: Evidence) -> float:
        """Of the cases called ``evidence``, the fraction that really were.

        This is the number that matters most for a filter: a false positive
        puts an unverifiable story in front of the reader wearing a badge that
        says it was checked.
        """
        called = [outcome for outcome in self.outcomes if outcome.actual is evidence]
        if not called:
            return 1.0
        return sum(1 for outcome in called if outcome.correct) / len(called)

    def recall(self, evidence: Evidence) -> float:
        """Of the cases that really were ``evidence``, the fraction found."""
        actual = [outcome for outcome in self.outcomes if outcome.case.expected is evidence]
        if not actual:
            return 1.0
        return sum(1 for outcome in actual if outcome.correct) / len(actual)

    def support(self, evidence: Evidence) -> int:
        """How many cases carry this label."""
        return sum(1 for outcome in self.outcomes if outcome.case.expected is evidence)

    @property
    def resolution_precision(self) -> float:
        """Precision of the binary question: is this checkable at all?

        A story wrongly promoted from ``none`` is the expensive mistake -- it
        reaches the reader. Calling a repo a paper is merely untidy.
        """
        promoted = [outcome for outcome in self.outcomes if outcome.actual is not Evidence.NONE]
        if not promoted:
            return 1.0
        return sum(1 for outcome in promoted if outcome.case.expected is not Evidence.NONE) / len(
            promoted
        )


def load_cases(path: Path | str = DEFAULT_CASES) -> list[Case]:
    """Read hand-labelled cases from a JSONL file.

    Raises:
        ValueError: on a line with an unknown label.
    """
    cases: list[Case] = []
    for number, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        try:
            expected = Evidence(payload["expected"])
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: unknown label {payload['expected']!r}") from exc
        cases.append(Case(url=payload["url"], expected=expected, note=payload.get("note", "")))
    return cases


def audit(cases: list[Case]) -> Report:
    """Classify every case and score the results."""
    return Report([Outcome(case=case, actual=classify(case.url).evidence) for case in cases])


def format_report(report: Report) -> str:
    """Render a report as a table, followed by every mistake."""
    lines = [
        f"{report.total} cases, {report.correct} correct",
        f"accuracy             {report.accuracy:6.1%}",
        f"resolution precision {report.resolution_precision:6.1%}"
        "   (of everything promoted above 'none')",
        "",
        f"{'EVIDENCE':<16}{'PRECISION':>10}{'RECALL':>9}{'N':>5}",
        "-" * 40,
    ]

    for evidence in Evidence:
        lines.append(
            f"{evidence.value:<16}"
            f"{report.precision(evidence):>10.1%}"
            f"{report.recall(evidence):>9.1%}"
            f"{report.support(evidence):>5}"
        )

    if report.mistakes:
        lines += ["", f"{len(report.mistakes)} misclassified:"]
        for outcome in report.mistakes:
            lines.append(
                f"  expected {outcome.case.expected.value:<14} "
                f"got {outcome.actual.value:<14} {outcome.case.url}"
            )
    else:
        lines += ["", "no misclassifications"]

    return "\n".join(lines)


# --- repository substance ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoCase:
    """One hand-judged repository."""

    slug: str
    vapor: bool
    facts: RepoFacts
    note: str = ""


@dataclass(frozen=True, slots=True)
class RepoOutcome:
    """What the substance rules said about a repository."""

    case: RepoCase
    substance: Substance

    @property
    def correct(self) -> bool:
        """True if the ``thin`` verdict matched the human judgement."""
        return self.substance.thin == self.case.vapor


@dataclass(frozen=True, slots=True)
class RepoReport:
    """Aggregate scoring over repository cases."""

    outcomes: list[RepoOutcome]

    @property
    def total(self) -> int:
        """How many repositories were judged."""
        return len(self.outcomes)

    @property
    def correct(self) -> int:
        """How many verdicts matched."""
        return sum(1 for outcome in self.outcomes if outcome.correct)

    @property
    def accuracy(self) -> float:
        """Fraction of verdicts that matched, 0-1."""
        return self.correct / self.total if self.total else 0.0

    @property
    def false_positives(self) -> list[RepoOutcome]:
        """Real projects called thin. The expensive mistake: it buries good work."""
        return [
            outcome
            for outcome in self.outcomes
            if not outcome.case.vapor and outcome.substance.thin
        ]

    @property
    def false_negatives(self) -> list[RepoOutcome]:
        """Launch pages that passed as real."""
        return [
            outcome
            for outcome in self.outcomes
            if outcome.case.vapor and not outcome.substance.thin
        ]


def load_repo_cases(path: Path | str = DEFAULT_REPO_CASES) -> list[RepoCase]:
    """Read hand-judged repository cases from a JSONL file."""
    cases: list[RepoCase] = []
    for number, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        verdict = payload.get("verdict")
        if verdict not in ("real", "vapor"):
            raise ValueError(f"{path}:{number}: verdict must be 'real' or 'vapor', got {verdict!r}")

        facts = dict(payload["facts"])
        for field_name in (
            "created_at",
            "pushed_at",
            "first_commit_at",
            "last_commit_at",
        ):
            if facts.get(field_name):
                facts[field_name] = parse_iso(facts[field_name])
            else:
                facts[field_name] = None

        cases.append(
            RepoCase(
                slug=payload["slug"],
                vapor=verdict == "vapor",
                facts=RepoFacts(slug=payload["slug"], **facts),
                note=payload.get("note", ""),
            )
        )
    return cases


def audit_repos(cases: list[RepoCase], now: datetime) -> RepoReport:
    """Apply the substance rules to every case and score the verdicts."""
    return RepoReport(
        [RepoOutcome(case=case, substance=assess_repo(case.facts, now)) for case in cases]
    )


def format_repo_report(report: RepoReport) -> str:
    """Render a repository report, listing both kinds of mistake separately."""
    lines = [
        f"{report.total} repositories, {report.correct} judged correctly",
        f"accuracy {report.accuracy:.1%}",
        "",
        f"{'REPOSITORY':<30}{'CALLED':<8}{'ACTUAL':<8} FLAGS",
        "-" * 78,
    ]

    for outcome in report.outcomes:
        called = "thin" if outcome.substance.thin else "real"
        actual = "thin" if outcome.case.vapor else "real"
        mark = " " if outcome.correct else "x"
        flags = ",".join(flag.value for flag in outcome.substance.flags) or "-"
        lines.append(f"{mark}{outcome.case.slug:<29}{called:<8}{actual:<8} {flags}")

    if report.false_positives:
        lines += ["", "real projects called thin (buries good work):"]
        lines += [
            f"  {outcome.case.slug}: {outcome.case.note}" for outcome in report.false_positives
        ]
    if report.false_negatives:
        lines += ["", "launch pages that passed as real:"]
        lines += [
            f"  {outcome.case.slug}: {outcome.case.note}" for outcome in report.false_negatives
        ]
    if not report.false_positives and not report.false_negatives:
        lines += ["", "every verdict matched"]

    return "\n".join(lines)
