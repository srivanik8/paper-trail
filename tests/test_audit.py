import json

import pytest

from papertrail.audit import (
    DEFAULT_CASES,
    Case,
    audit,
    format_report,
    load_cases,
)
from papertrail.provenance import Evidence

#: The shipped case set must not regress below this. Raise it when the set
#: grows; never lower it to make a change pass.
MIN_ACCURACY = 1.0


def test_the_shipped_cases_load():
    cases = load_cases()
    assert len(cases) >= 60
    assert all(isinstance(case.expected, Evidence) for case in cases)


def test_the_classifier_still_agrees_with_every_hand_label():
    report = audit(load_cases())
    assert report.accuracy >= MIN_ACCURACY, format_report(report)


def test_the_case_set_covers_every_evidence_type():
    report = audit(load_cases())
    for evidence in Evidence:
        assert report.support(evidence) > 0, f"no cases labelled {evidence.value}"


def test_negatives_outnumber_any_single_positive_class():
    """Most of the web is not a primary source; the case set should reflect that."""
    report = audit(load_cases())
    negatives = report.support(Evidence.NONE)
    for evidence in Evidence:
        if evidence is not Evidence.NONE:
            assert negatives > report.support(evidence)


def test_lab_domains_are_covered_by_both_a_positive_and_a_negative():
    """The careers-page false positive must stay pinned by a case."""
    urls = {case.url for case in load_cases()}
    assert any("openai.com/careers" in url for url in urls)
    assert any("openai.com/index" in url for url in urls)


# --- the report itself ------------------------------------------------------


def make_report(pairs: list[tuple[str, Evidence]]):
    return audit([Case(url=url, expected=expected) for url, expected in pairs])


def test_accuracy_counts_every_class():
    report = make_report(
        [
            ("https://arxiv.org/abs/2401.00001", Evidence.PAPER),
            ("https://example.com/nothing", Evidence.NONE),
        ]
    )
    assert report.accuracy == 1.0
    assert report.mistakes == []


def test_a_mistake_is_reported_with_both_labels():
    report = make_report([("https://arxiv.org/abs/2401.00001", Evidence.NONE)])

    assert report.accuracy == 0.0
    (mistake,) = report.mistakes
    assert mistake.case.expected is Evidence.NONE
    assert mistake.actual is Evidence.PAPER


def test_resolution_precision_ignores_confusions_between_positive_classes():
    """Calling a repo a paper is untidy; promoting nothing to something is not."""
    report = make_report([("https://github.com/owner/repo", Evidence.PAPER)])
    assert report.accuracy == 0.0
    assert report.resolution_precision == 1.0


def test_resolution_precision_catches_a_promotion_from_nothing():
    report = make_report([("https://arxiv.org/abs/2401.00001", Evidence.NONE)])
    assert report.resolution_precision == 0.0


def test_precision_and_recall_of_an_absent_class_are_one():
    report = make_report([("https://example.com/x", Evidence.NONE)])
    assert report.precision(Evidence.PAPER) == 1.0
    assert report.recall(Evidence.PAPER) == 1.0


def test_an_empty_report_does_not_divide_by_zero():
    report = audit([])
    assert report.accuracy == 0.0
    assert report.resolution_precision == 1.0


def test_unknown_labels_are_rejected_with_the_line_number(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"url": "https://example.com", "expected": "nonsense"}) + "\n")

    with pytest.raises(ValueError, match=r"cases.jsonl:1: unknown label 'nonsense'"):
        load_cases(path)


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"url": "https://arxiv.org/abs/1", "expected": "paper"}) + "\n\n\n")
    assert len(load_cases(path)) == 1


def test_the_rendered_report_names_every_mistake():
    text = format_report(make_report([("https://arxiv.org/abs/2401.00001", Evidence.NONE)]))
    assert "1 misclassified" in text
    assert "https://arxiv.org/abs/2401.00001" in text
    assert "expected none" in text


def test_a_clean_report_says_so():
    assert "no misclassifications" in format_report(
        make_report([("https://arxiv.org/abs/1", Evidence.PAPER)])
    )


def test_the_default_case_file_ships_with_the_project():
    assert DEFAULT_CASES.exists()
