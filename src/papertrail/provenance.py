"""What kind of evidence a URL is, if any.

This is the project's whole thesis in one module. Nothing here judges whether a
claim is *true* -- no model can. It answers a narrower question that is actually
computable: **can this item point at something you could go and check?** A paper,
a repository, published weights, a post from the lab that did the work.

Classification is pure and offline: it inspects the URL and nothing else. The
network half -- fetching a page to find the links it points at -- lives in
:mod:`papertrail.fetcher` and :mod:`papertrail.resolver`, which both call in
here to decide what they found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from .ids import canonical_url


class Evidence(StrEnum):
    """What a URL can be checked against.

    Ordered weakest-last in :data:`EVIDENCE_STRENGTH`, which is what decides
    between two candidates found on the same page.
    """

    PAPER = "paper"
    REPO = "repo"
    MODEL_WEIGHTS = "model_weights"
    OFFICIAL_BLOG = "official_blog"
    NONE = "none"


#: Preference order when a page offers several candidates. A paper and a repo
#: can both be verified in detail; a lab blog post is the weakest of the four
#: because the lab is the interested party -- it is evidence that an
#: announcement was made, not that the thing works.
EVIDENCE_STRENGTH: dict[Evidence, int] = {
    Evidence.PAPER: 4,
    Evidence.REPO: 3,
    Evidence.MODEL_WEIGHTS: 2,
    Evidence.OFFICIAL_BLOG: 1,
    Evidence.NONE: 0,
}


@dataclass(frozen=True, slots=True)
class Provenance:
    """A classification result.

    Attributes:
        evidence: What kind of artifact ``url`` is.
        url: The canonical primary-source URL, or ``None`` for
            :attr:`Evidence.NONE`.
        via: How it was found -- ``"self"`` when the item's own URL was already
            primary, ``"page"`` when it was extracted from the page's links,
            ``"source"`` when the ingester supplied it.
    """

    evidence: Evidence
    url: str | None = None
    via: str = "self"

    @property
    def resolved(self) -> bool:
        """True if this points at something checkable."""
        return self.evidence is not Evidence.NONE

    @property
    def strength(self) -> int:
        """Rank used to choose between candidates."""
        return EVIDENCE_STRENGTH[self.evidence]


NONE = Provenance(evidence=Evidence.NONE, url=None, via="none")

# --- papers -----------------------------------------------------------------

_PAPER_HOSTS = {
    "arxiv.org": re.compile(r"^/abs/[^/]+$"),
    "openreview.net": re.compile(r"^/forum$|^/pdf$"),
    "aclanthology.org": re.compile(r"^/[\w.\-]+/?$"),
    "proceedings.mlr.press": re.compile(r"^/v\d+/"),
    "papers.nips.cc": re.compile(r"^/paper"),
    "dl.acm.org": re.compile(r"^/doi/"),
    "biorxiv.org": re.compile(r"^/content/"),
    "doi.org": re.compile(r"^/10\."),
}

# --- code -------------------------------------------------------------------

_CODE_HOSTS = frozenset({"github.com", "gitlab.com", "codeberg.org", "bitbucket.org"})

#: First path segments on a code host that are the site itself, not an account.
_CODE_RESERVED = frozenset(
    {
        "about",
        "account",
        "apps",
        "blog",
        "collections",
        "contact",
        "customer-stories",
        "dashboard",
        "discussions",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "join",
        "login",
        "logout",
        "marketplace",
        "new",
        "notifications",
        "orgs",
        "organizations",
        "pricing",
        "pulls",
        "readme",
        "search",
        "security",
        "settings",
        "showcase",
        "signup",
        "sponsors",
        "stars",
        "topics",
        "trending",
        "users",
        "watching",
    }
)

# --- weights ----------------------------------------------------------------

_HF_HOST = "huggingface.co"
#: Namespaces on the model hub that are not a model or dataset repository.
_HF_RESERVED = frozenset(
    {"blog", "docs", "join", "learn", "login", "papers", "posts", "pricing", "spaces", "tasks"}
)

# --- lab blogs --------------------------------------------------------------

#: Hosts whose posts count as an official announcement. A path pattern narrows
#: the general-purpose domains to their research sections.
_BLOG_HOSTS: dict[str, re.Pattern[str] | None] = {
    "ai.meta.com": None,
    "ai.googleblog.com": None,
    "allenai.org": None,
    "anthropic.com": None,
    "cohere.com": None,
    "deepmind.com": None,
    "deepmind.google": None,
    "deepseek.com": None,
    "eleuther.ai": None,
    "mistral.ai": None,
    "openai.com": None,
    "qwenlm.github.io": None,
    "research.google": None,
    "stability.ai": None,
    "together.ai": None,
    "blog.google": re.compile(r"^/technology/"),
    "databricks.com": re.compile(r"^/blog/"),
    "microsoft.com": re.compile(r"^/en-us/research/"),
    "nvidia.com": re.compile(r"^/en-us/(?:research|ai)/"),
}


def _host_and_path(url: str) -> tuple[str, str]:
    """Split a canonicalized URL into host and path."""
    parts = urlsplit(canonical_url(url))
    return parts.netloc.split(":")[0], parts.path or "/"


def _matches(host: str, table: dict[str, re.Pattern[str] | None], path: str) -> bool:
    """True if ``host`` is in ``table`` and its pattern (if any) matches ``path``."""
    for candidate, pattern in table.items():
        if host == candidate or host.endswith(f".{candidate}"):
            return pattern is None or bool(pattern.search(path))
    return False


def repo_slug(url: str) -> str | None:
    """Return ``owner/repo`` if ``url`` addresses a repository, else ``None``.

    Deep links normalize to the repository root: a link to a file, a release or
    an issue is still a link to that repo, and day 4 needs the slug rather than
    the page.
    """
    host, path = _host_and_path(url)
    if host not in _CODE_HOSTS:
        return None

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None

    owner, repo = segments[0], segments[1]
    if owner.lower() in _CODE_RESERVED or repo.lower() in _CODE_RESERVED:
        return None
    return f"{owner}/{repo}"


def classify(url: str, via: str = "self") -> Provenance:
    """Classify a URL as evidence.

    Returns :data:`NONE` for anything that is not a paper, a repository,
    published weights or an official lab post -- which is most of the web, and
    is the point.
    """
    if not url or not url.strip():
        return NONE

    host, path = _host_and_path(url)
    if not host:
        return NONE

    if _matches(host, _PAPER_HOSTS, path):
        return Provenance(Evidence.PAPER, canonical_url(url), via)

    # A Hugging Face paper page is a paper; a model or dataset page is weights.
    if host == _HF_HOST or host.endswith(f".{_HF_HOST}"):
        segments = [segment for segment in path.split("/") if segment]
        if segments and segments[0] == "papers" and len(segments) > 1:
            return Provenance(Evidence.PAPER, f"https://arxiv.org/abs/{segments[1]}", via)
        if len(segments) >= 2 and segments[0].lower() not in _HF_RESERVED:
            return Provenance(Evidence.MODEL_WEIGHTS, canonical_url(url), via)
        if len(segments) >= 3 and segments[0] == "datasets":
            return Provenance(Evidence.MODEL_WEIGHTS, canonical_url(url), via)

    slug = repo_slug(url)
    if slug is not None:
        return Provenance(Evidence.REPO, f"https://{host}/{slug}", via)

    if _matches(host, _BLOG_HOSTS, path):
        return Provenance(Evidence.OFFICIAL_BLOG, canonical_url(url), via)

    return NONE


def best(candidates: list[Provenance]) -> Provenance:
    """Pick the strongest classification, preserving input order among equals."""
    strongest = NONE
    for candidate in candidates:
        if candidate.strength > strongest.strength:
            strongest = candidate
    return strongest
