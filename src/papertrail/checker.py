"""Turning a resolved primary source into a judgement about it.

:mod:`papertrail.resolver` establishes that a story points at a repository or a
paper. This asks whether that artifact holds up, by dispatching on the evidence
type to whichever fact-gatherer knows how to look.

Checks are **advisory**. A thin repository does not remove a story from the
digest -- it annotates it, and day 5's scoring decides what to do with the
annotation. Dropping on provenance is a claim about whether evidence exists;
dropping on substance would be a claim about quality, and that is a judgement
call the rubric should make with the full picture.
"""

from __future__ import annotations

from datetime import datetime

from .github import GitHub
from .papers import ArxivPapers, arxiv_id
from .provenance import Evidence, Provenance, repo_slug
from .substance import Substance, assess_paper, assess_repo
from .timeutil import utcnow


class Checker:
    """Assesses whatever a story's provenance points at.

    Args:
        github: Repository fact gatherer. ``None`` skips repository checks.
        papers: Paper fact gatherer. ``None`` skips paper checks.
    """

    def __init__(self, github: GitHub | None = None, papers: ArxivPapers | None = None) -> None:
        self.github = github
        self.papers = papers

    @property
    def requests(self) -> int:
        """How many API calls the run has spent on checking."""
        return (self.github.requests if self.github else 0) + (
            self.papers.requests if self.papers else 0
        )

    def check(self, provenance: Provenance, now: datetime | None = None) -> Substance:
        """Assess a resolved primary source.

        Returns an empty :class:`Substance` for evidence nobody can check --
        published weights and lab posts have no equivalent of a commit history,
        so there is nothing honest to say about them here.
        """
        if not provenance.resolved or not provenance.url:
            return Substance()

        moment = now or utcnow()

        if provenance.evidence is Evidence.REPO and self.github is not None:
            slug = repo_slug(provenance.url)
            if slug is not None:
                return assess_repo(self.github.facts(slug, now=moment), moment)

        if (
            provenance.evidence is Evidence.PAPER
            and self.papers is not None
            and arxiv_id(provenance.url) is not None
        ):
            return assess_paper(self.papers.facts(provenance.url, now=moment))

        return Substance()
