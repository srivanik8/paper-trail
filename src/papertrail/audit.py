"""Scoring the classifier against hand-labelled cases.

Day 3's acceptance criterion is a number, not a feeling: hand-check a set of
real items and state the resolver's precision. This module is how that number
gets produced, and re-produced on every commit.

**What it does and does not measure.** The cases in ``data/provenance_cases.jsonl``
are hand-labelled URL *shapes* drawn from real-world patterns. Scoring against
them tells you whether :func:`papertrail.provenance.classify` still agrees with
a human's judgement about what each URL is -- which is a regression guard and a
calibration check. It is not a field measurement of live pages; getting that
means pointing :class:`papertrail.resolver.Resolver` at a day of real traffic
and labelling what comes back. Extend the file when you do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .provenance import Evidence, classify

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "data" / "provenance_cases.jsonl"


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
