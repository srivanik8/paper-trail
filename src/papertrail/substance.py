"""Does the artifact behind a claim hold up when you look at it?

Provenance says a story points at a repository. This says whether that
repository is a real project or a README with a waitlist link. The two are
different questions and the second is the one that catches hype: anyone can put
a GitHub URL in a launch post.

Everything here is a **pure function over facts**. Fetching the facts is
:mod:`papertrail.github` and :mod:`papertrail.sources.arxiv`; deciding what they
mean is here, so the rules can be read, argued with, and tested without a
network.

The flag vocabulary is closed on purpose. Free-text findings cannot be counted,
compared across months, or fed to a rubric -- and a month of counted flags is
the point of keeping the rejects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from .timeutil import to_utc


class Flag(StrEnum):
    """The closed vocabulary of substance findings.

    A flag is an observation, not a verdict. A single contributor is normal for
    a good research release; a single contributor *and* four days of history
    *and* nothing but a README is a launch page.
    """

    # repositories
    README_ONLY = "readme_only"
    SINGLE_CONTRIBUTOR = "single_contributor"
    YOUNG_HISTORY = "young_history"
    STALE = "stale"
    NO_LICENSE = "no_license"
    WAITLIST = "waitlist"
    ARCHIVED = "archived"
    FORK = "fork"
    # papers
    WITHDRAWN = "withdrawn"
    UNREVISED = "unrevised"
    SINGLE_AUTHOR = "single_author"


#: History shorter than this is too new to have been maintained.
YOUNG_HISTORY_DAYS = 7

#: No push in this long and the project has stopped, whatever its star count.
STALE_DAYS = 180

#: Enough implementation to count as a project on its own, regardless of how
#: it got there. A lab publishing months of work as a single commit on paper
#: day is one author with a week of history and is emphatically not a launch
#: page -- the code is right there.
SUBSTANTIAL_CODE_FILES = 20

#: Phrases that mean "there is nothing here yet, but leave your email".
_WAITLIST = re.compile(
    r"""
    join \s+ (the \s+)? (waitlist|beta|early \s+ access)
    | (waitlist|early \s+ access) \b
    | request \s+ (early \s+)? access
    | sign \s* up \s+ for \s+ (early \s+)? access
    | (typeform\.com|tally\.so|airtable\.com/shr|forms\.gle)
    | coming \s+ soon
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Extensions that are documentation rather than an implementation.
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc", ".pdf", ".png", ".jpg", ".svg", ".gif")

#: Paths that exist in every repository and prove nothing about it.
_BOILERPLATE = (
    ".gitignore",
    ".gitattributes",
    "license",
    "licence",
    "notice",
    "codeowners",
)


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """What the GitHub API says about a repository.

    Timestamps are aware UTC. ``None`` means "not retrieved", which is not the
    same as zero -- a rule that depends on a missing fact does not fire.
    """

    slug: str
    stars: int = 0
    created_at: datetime | None = None
    pushed_at: datetime | None = None
    first_commit_at: datetime | None = None
    last_commit_at: datetime | None = None
    contributors: int | None = None
    code_files: int | None = None
    doc_files: int = 0
    has_license: bool = True
    archived: bool = False
    is_fork: bool = False
    description: str = ""
    readme: str = ""
    error: str | None = None

    @property
    def retrieved(self) -> bool:
        """True if the facts were actually fetched."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class PaperFacts:
    """What arXiv says about a submission."""

    arxiv_id: str
    version: int = 1
    withdrawn: bool = False
    authors: int = 0
    categories: tuple[str, ...] = ()
    published_at: datetime | None = None
    updated_at: datetime | None = None
    error: str | None = None

    @property
    def retrieved(self) -> bool:
        """True if the facts were actually fetched."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class Substance:
    """The verdict on one artifact."""

    flags: tuple[Flag, ...] = ()
    star_velocity: float | None = None
    code_files: int | None = None
    notes: dict[str, object] = field(default_factory=dict)

    @property
    def thin(self) -> bool:
        """True if the combination of flags says there is nothing here yet.

        Deliberately a combination, never a single flag. Every individual flag
        below is normal somewhere: plenty of excellent research code is one
        author with a week of history, and plenty of finished projects are
        archived.
        """
        found = set(self.flags)
        if Flag.WAITLIST in found:
            return True
        if Flag.README_ONLY in found:
            return True
        if {Flag.SINGLE_CONTRIBUTOR, Flag.YOUNG_HISTORY} <= found:
            # ...unless there is a real implementation sitting there. Judging a
            # code drop by its commit history is how you bury a paper release.
            return self.code_files is None or self.code_files < SUBSTANTIAL_CODE_FILES
        return {Flag.ARCHIVED, Flag.STALE} <= found


def code_and_doc_files(paths: list[str]) -> tuple[int, int]:
    """Split a repository's file list into implementation and documentation.

    Boilerplate present in every repository counts as neither: a project whose
    only files are a README and a licence is still a README.
    """
    code = docs = 0
    for path in paths:
        name = path.rsplit("/", 1)[-1].lower()
        if any(name.startswith(prefix) for prefix in _BOILERPLATE):
            continue
        if name.endswith(_DOC_SUFFIXES):
            docs += 1
        else:
            code += 1
    return code, docs


def star_velocity(stars: int, created_at: datetime | None, now: datetime) -> float | None:
    """Stars per day since creation -- the honest version of "trending".

    A repository with 4,000 stars is unremarkable at four years old and
    extraordinary at four days.
    """
    if created_at is None:
        return None
    days = max((to_utc(now) - to_utc(created_at)).total_seconds() / 86400, 1.0)
    return stars / days


def repo_flags(facts: RepoFacts, now: datetime) -> list[Flag]:
    """Apply the repository rules to ``facts``, in vocabulary order."""
    flags: list[Flag] = []

    if facts.code_files is not None and facts.code_files == 0:
        flags.append(Flag.README_ONLY)

    if facts.contributors is not None and facts.contributors <= 1:
        flags.append(Flag.SINGLE_CONTRIBUTOR)

    if facts.first_commit_at is not None and facts.last_commit_at is not None:
        span = to_utc(facts.last_commit_at) - to_utc(facts.first_commit_at)
        if span < timedelta(days=YOUNG_HISTORY_DAYS):
            flags.append(Flag.YOUNG_HISTORY)

    latest = facts.last_commit_at or facts.pushed_at
    if latest is not None and to_utc(now) - to_utc(latest) > timedelta(days=STALE_DAYS):
        flags.append(Flag.STALE)

    if not facts.has_license:
        flags.append(Flag.NO_LICENSE)

    if _WAITLIST.search(f"{facts.description}\n{facts.readme}"):
        flags.append(Flag.WAITLIST)

    if facts.archived:
        flags.append(Flag.ARCHIVED)

    if facts.is_fork:
        flags.append(Flag.FORK)

    return flags


def paper_flags(facts: PaperFacts) -> list[Flag]:
    """Apply the paper rules to ``facts``."""
    flags: list[Flag] = []

    if facts.withdrawn:
        flags.append(Flag.WITHDRAWN)
    if facts.version <= 1:
        flags.append(Flag.UNREVISED)
    if facts.authors == 1:
        flags.append(Flag.SINGLE_AUTHOR)

    return flags


def assess_repo(facts: RepoFacts, now: datetime) -> Substance:
    """Flags plus star velocity for a repository."""
    if not facts.retrieved:
        return Substance(notes={"error": facts.error})

    return Substance(
        flags=tuple(repo_flags(facts, now)),
        star_velocity=star_velocity(facts.stars, facts.created_at, now),
        code_files=facts.code_files,
        notes={
            "slug": facts.slug,
            "stars": facts.stars,
            "contributors": facts.contributors,
            "code_files": facts.code_files,
        },
    )


def assess_paper(facts: PaperFacts) -> Substance:
    """Flags for a paper. arXiv has no popularity signal, so no velocity."""
    if not facts.retrieved:
        return Substance(notes={"error": facts.error})

    return Substance(
        flags=tuple(paper_flags(facts)),
        notes={
            "arxiv_id": facts.arxiv_id,
            "version": facts.version,
            "authors": facts.authors,
        },
    )
