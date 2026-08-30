from datetime import UTC, datetime, timedelta

import pytest

from papertrail.substance import (
    STALE_DAYS,
    YOUNG_HISTORY_DAYS,
    Flag,
    PaperFacts,
    RepoFacts,
    Substance,
    assess_paper,
    assess_repo,
    code_and_doc_files,
    paper_flags,
    repo_flags,
    star_velocity,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def solid(**overrides) -> RepoFacts:
    """A repository with nothing wrong with it."""
    facts = {
        "slug": "owner/repo",
        "stars": 900,
        "created_at": NOW - timedelta(days=800),
        "pushed_at": NOW - timedelta(days=2),
        "first_commit_at": NOW - timedelta(days=800),
        "last_commit_at": NOW - timedelta(days=2),
        "contributors": 47,
        "code_files": 260,
        "doc_files": 12,
        "has_license": True,
        "description": "A fast inference runtime.",
        "readme": "Build with cmake. Contributions welcome.",
    }
    facts.update(overrides)
    return RepoFacts(**facts)


def test_a_healthy_repository_raises_no_flags():
    assert repo_flags(solid(), NOW) == []
    assert assess_repo(solid(), NOW).thin is False


# --- individual rules -------------------------------------------------------


def test_readme_only_fires_when_there_is_no_implementation():
    assert Flag.README_ONLY in repo_flags(solid(code_files=0), NOW)


def test_single_contributor():
    assert Flag.SINGLE_CONTRIBUTOR in repo_flags(solid(contributors=1), NOW)
    assert Flag.SINGLE_CONTRIBUTOR not in repo_flags(solid(contributors=2), NOW)


def test_young_history_measures_the_span_not_the_age():
    young = solid(
        first_commit_at=NOW - timedelta(days=3),
        last_commit_at=NOW - timedelta(days=1),
    )
    assert Flag.YOUNG_HISTORY in repo_flags(young, NOW)

    old_but_brief = solid(
        first_commit_at=NOW - timedelta(days=400),
        last_commit_at=NOW - timedelta(days=399),
    )
    assert Flag.YOUNG_HISTORY in repo_flags(old_but_brief, NOW)


def test_a_span_at_the_boundary_is_not_young():
    facts = solid(
        first_commit_at=NOW - timedelta(days=YOUNG_HISTORY_DAYS + 1),
        last_commit_at=NOW - timedelta(days=1),
    )
    assert Flag.YOUNG_HISTORY not in repo_flags(facts, NOW)


def test_stale_fires_when_nothing_has_landed_in_months():
    facts = solid(last_commit_at=NOW - timedelta(days=STALE_DAYS + 1))
    assert Flag.STALE in repo_flags(facts, NOW)


def test_stale_falls_back_to_pushed_at_when_commits_are_unknown():
    facts = solid(last_commit_at=None, pushed_at=NOW - timedelta(days=STALE_DAYS + 10))
    assert Flag.STALE in repo_flags(facts, NOW)


def test_no_license():
    assert Flag.NO_LICENSE in repo_flags(solid(has_license=False), NOW)


@pytest.mark.parametrize(
    "text",
    [
        "Join the waitlist for early access",
        "Request early access here",
        "Sign up for access at https://tally.so/r/abc",
        "Coming soon!",
        "Fill in https://someone.typeform.com/to/xyz",
        "Early access is open",
    ],
)
def test_waitlist_language_is_caught_in_the_readme(text):
    assert Flag.WAITLIST in repo_flags(solid(readme=text), NOW)


def test_waitlist_language_is_caught_in_the_description():
    assert Flag.WAITLIST in repo_flags(solid(description="Join the beta"), NOW)


def test_ordinary_readmes_do_not_trip_the_waitlist_rule():
    for text in [
        "Install with pip. See the docs for access control configuration.",
        "We welcome contributions; open an issue first.",
        "Access tokens are read from the environment.",
    ]:
        assert Flag.WAITLIST not in repo_flags(solid(readme=text), NOW)


def test_archived_and_fork():
    assert Flag.ARCHIVED in repo_flags(solid(archived=True), NOW)
    assert Flag.FORK in repo_flags(solid(is_fork=True), NOW)


def test_missing_facts_do_not_fire_rules():
    """None means not retrieved, which is not the same as zero."""
    unknown = RepoFacts(slug="owner/repo")
    flags = repo_flags(unknown, NOW)

    assert Flag.README_ONLY not in flags
    assert Flag.SINGLE_CONTRIBUTOR not in flags
    assert Flag.YOUNG_HISTORY not in flags
    assert Flag.STALE not in flags


# --- the combination --------------------------------------------------------


def test_one_contributor_alone_is_not_thin():
    """Plenty of good research code is one author."""
    assert assess_repo(solid(contributors=1), NOW).thin is False


def test_a_short_history_alone_is_not_thin():
    facts = solid(first_commit_at=NOW - timedelta(days=2), last_commit_at=NOW)
    assert assess_repo(facts, NOW).thin is False


def test_one_contributor_and_a_few_days_together_are_thin():
    facts = solid(
        contributors=1,
        first_commit_at=NOW - timedelta(days=2),
        last_commit_at=NOW,
    )
    assert assess_repo(facts, NOW).thin is True


def test_a_readme_with_a_waitlist_is_thin_on_its_own():
    assert assess_repo(solid(readme="Join the waitlist"), NOW).thin is True


def test_no_implementation_is_thin_on_its_own():
    assert assess_repo(solid(code_files=0), NOW).thin is True


def test_archived_and_stale_together_are_thin():
    facts = solid(archived=True, last_commit_at=NOW - timedelta(days=STALE_DAYS + 1))
    assert assess_repo(facts, NOW).thin is True


def test_an_archived_but_recently_active_project_is_not_thin():
    assert assess_repo(solid(archived=True), NOW).thin is False


def test_facts_that_could_not_be_retrieved_are_not_thin():
    """Absence of evidence is not evidence of absence."""
    result = assess_repo(RepoFacts(slug="owner/repo", error="404"), NOW)
    assert result.thin is False
    assert result.flags == ()
    assert result.notes["error"] == "404"


# --- file classification ----------------------------------------------------


def test_code_and_docs_are_separated():
    code, docs = code_and_doc_files(["src/main.py", "README.md", "docs/guide.rst", "Makefile"])
    assert (code, docs) == (2, 2)


def test_boilerplate_counts_as_neither():
    """A README plus a licence is still just a README."""
    code, docs = code_and_doc_files(["README.md", "LICENSE", ".gitignore", "CODEOWNERS"])
    assert (code, docs) == (0, 1)


def test_images_count_as_documentation_not_code():
    code, docs = code_and_doc_files(["banner.png", "demo.gif", "paper.pdf"])
    assert (code, docs) == (0, 3)


def test_an_empty_tree_is_all_zeroes():
    assert code_and_doc_files([]) == (0, 0)


# --- star velocity ----------------------------------------------------------


def test_velocity_divides_stars_by_age():
    assert star_velocity(4000, NOW - timedelta(days=1000), NOW) == pytest.approx(4.0, rel=0.01)


def test_the_same_star_count_is_remarkable_when_young():
    old = star_velocity(4000, NOW - timedelta(days=1460), NOW)
    new = star_velocity(4000, NOW - timedelta(days=4), NOW)
    assert new > old * 100


def test_velocity_never_divides_by_zero():
    assert star_velocity(50, NOW, NOW) == 50.0


def test_velocity_is_unknown_without_a_creation_date():
    assert star_velocity(50, None, NOW) is None


# --- papers -----------------------------------------------------------------


def test_a_revised_multi_author_paper_raises_nothing():
    facts = PaperFacts(arxiv_id="2401.00001", version=3, authors=6)
    assert paper_flags(facts) == []


def test_a_withdrawn_paper_is_flagged():
    assert Flag.WITHDRAWN in paper_flags(PaperFacts(arxiv_id="1", withdrawn=True, version=2))


def test_a_first_version_is_flagged_as_unrevised():
    assert Flag.UNREVISED in paper_flags(PaperFacts(arxiv_id="1", version=1, authors=4))
    assert Flag.UNREVISED not in paper_flags(PaperFacts(arxiv_id="1", version=2, authors=4))


def test_a_single_author_paper_is_flagged():
    assert Flag.SINGLE_AUTHOR in paper_flags(PaperFacts(arxiv_id="1", version=2, authors=1))


def test_paper_assessment_carries_its_notes():
    result = assess_paper(PaperFacts(arxiv_id="2401.00001", version=2, authors=6))
    assert result.notes["version"] == 2
    assert result.star_velocity is None


def test_unretrieved_paper_facts_yield_nothing():
    result = assess_paper(PaperFacts(arxiv_id="1", error="timeout"))
    assert result.flags == ()
    assert result.notes["error"] == "timeout"


def test_substance_defaults_are_empty():
    assert Substance().flags == ()
    assert Substance().thin is False
