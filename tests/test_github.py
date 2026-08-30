import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from papertrail.github import GitHub
from papertrail.store import Store

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SLUG = "owner/repo"

REPO = {
    "full_name": "owner/repo",
    "stargazers_count": 4200,
    "created_at": "2024-01-01T00:00:00Z",
    "pushed_at": "2026-05-30T00:00:00Z",
    "default_branch": "main",
    "license": {"spdx_id": "MIT"},
    "archived": False,
    "fork": False,
    "description": "A fast inference runtime.",
}

TREE = {
    "tree": [
        {"type": "blob", "path": "README.md"},
        {"type": "blob", "path": "LICENSE"},
        {"type": "blob", "path": "src/main.c"},
        {"type": "blob", "path": "src/model.c"},
        {"type": "tree", "path": "src"},
    ]
}


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def api(
    repo: dict | None = REPO,
    contributors_last_page: int | None = 47,
    contributors_body: list | None = None,
    newest_commit: str | None = "2026-05-30T09:00:00Z",
    commits_last_page: int | None = 900,
    oldest_commit: str | None = "2024-01-01T09:00:00Z",
    tree: dict | None = None,
    readme: str | None = "Build with cmake.",
    log: list[str] | None = None,
) -> httpx.Client:
    """A client answering the four endpoints the gatherer calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if log is not None:
            log.append(url)

        if url.endswith(f"/repos/{SLUG}"):
            return httpx.Response(200, json=repo) if repo else httpx.Response(404, json={})

        if "/contributors" in url:
            headers = {}
            if contributors_last_page:
                headers["link"] = (
                    f"<https://api.github.com/repos/{SLUG}/contributors?per_page=1"
                    f'&page={contributors_last_page}>; rel="last"'
                )
            body = contributors_body if contributors_body is not None else [{"login": "a"}]
            return httpx.Response(200, json=body, headers=headers)

        if "/commits" in url:
            if "page=" in url and f"page={commits_last_page}" in url:
                return httpx.Response(
                    200, json=[{"commit": {"committer": {"date": oldest_commit}}}]
                )
            headers = {}
            if commits_last_page:
                headers["link"] = (
                    f"<https://api.github.com/repos/{SLUG}/commits?per_page=1"
                    f'&page={commits_last_page}>; rel="last"'
                )
            if newest_commit is None:
                return httpx.Response(200, json=[], headers=headers)
            return httpx.Response(
                200, json=[{"commit": {"committer": {"date": newest_commit}}}], headers=headers
            )

        if "/git/trees/" in url:
            return httpx.Response(200, json=tree if tree is not None else TREE)

        if url.endswith("/readme"):
            if readme is None:
                return httpx.Response(404, json={})
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(readme.encode()).decode(),
                },
            )

        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_repository_metadata_is_mapped(store):
    facts = GitHub(store, client=api()).facts(SLUG, now=NOW)

    assert facts.slug == "owner/repo"
    assert facts.stars == 4200
    assert facts.created_at == datetime(2024, 1, 1, tzinfo=UTC)
    assert facts.has_license is True
    assert facts.archived is False
    assert facts.description == "A fast inference runtime."
    assert facts.retrieved is True


def test_contributors_are_counted_from_the_link_header(store):
    """One request, not a walk of a 3,000-name list."""
    facts = GitHub(store, client=api(contributors_last_page=47)).facts(SLUG, now=NOW)
    assert facts.contributors == 47


def test_a_single_page_of_contributors_is_counted_directly(store):
    client = api(contributors_last_page=None, contributors_body=[{"login": "a"}, {"login": "b"}])
    assert GitHub(store, client=client).facts(SLUG, now=NOW).contributors == 2


def test_the_commit_span_uses_the_first_and_last_pages(store):
    facts = GitHub(store, client=api()).facts(SLUG, now=NOW)

    assert facts.last_commit_at == datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
    assert facts.first_commit_at == datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def test_a_single_page_of_commits_makes_first_and_last_the_same(store):
    client = api(commits_last_page=None)
    facts = GitHub(store, client=client).facts(SLUG, now=NOW)
    assert facts.first_commit_at == facts.last_commit_at


def test_an_empty_repository_has_no_commit_span(store):
    facts = GitHub(store, client=api(newest_commit=None)).facts(SLUG, now=NOW)
    assert facts.first_commit_at is None
    assert facts.last_commit_at is None


def test_the_tree_separates_code_from_documentation(store):
    facts = GitHub(store, client=api()).facts(SLUG, now=NOW)
    assert facts.code_files == 2
    assert facts.doc_files == 1


def test_a_readme_only_repository_reports_zero_code_files(store):
    tree = {"tree": [{"type": "blob", "path": "README.md"}, {"type": "blob", "path": "LICENSE"}]}
    facts = GitHub(store, client=api(tree=tree)).facts(SLUG, now=NOW)
    assert facts.code_files == 0


def test_the_readme_is_decoded(store):
    facts = GitHub(store, client=api(readme="Join the waitlist")).facts(SLUG, now=NOW)
    assert facts.readme == "Join the waitlist"


def test_a_missing_readme_is_empty_not_an_error(store):
    facts = GitHub(store, client=api(readme=None)).facts(SLUG, now=NOW)
    assert facts.readme == ""
    assert facts.retrieved is True


def test_a_missing_repository_is_recorded_as_an_error(store):
    facts = GitHub(store, client=api(repo=None)).facts(SLUG, now=NOW)
    assert facts.retrieved is False
    assert "404" in facts.error


def test_a_network_failure_is_recorded_not_raised(store):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        facts = GitHub(store, client=client).facts(SLUG, now=NOW)

    assert facts.retrieved is False
    assert "ConnectTimeout" in facts.error


def test_optional_endpoints_failing_do_not_lose_the_repository(store):
    """Metadata is enough to judge on; the rest degrade to None."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(f"/repos/{SLUG}"):
            return httpx.Response(200, json=REPO)
        return httpx.Response(500, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        facts = GitHub(store, client=client).facts(SLUG, now=NOW)

    assert facts.retrieved is True
    assert facts.stars == 4200
    assert facts.contributors is None
    assert facts.code_files is None


# --- caching ----------------------------------------------------------------


def test_the_second_lookup_is_served_from_cache(store):
    log: list[str] = []
    github = GitHub(store, client=api(log=log))

    github.facts(SLUG, now=NOW)
    used = github.requests
    facts = github.facts(SLUG, now=NOW)

    assert github.requests == used
    assert facts.stars == 4200


def test_a_stale_cache_entry_is_refetched(store):
    github = GitHub(store, client=api(), max_age=timedelta(hours=1))

    github.facts(SLUG, now=NOW)
    used = github.requests
    github.facts(SLUG, now=NOW + timedelta(days=2))

    assert github.requests > used


def test_every_field_survives_the_cache_round_trip(store):
    github = GitHub(store, client=api(readme="Build with cmake."))
    fresh = github.facts(SLUG, now=NOW)
    cached = github.facts(SLUG, now=NOW)

    assert cached == fresh


def test_a_corrupt_cache_entry_is_reported_not_crashed(store):
    store.cache_page(f"github-facts:{SLUG}", status=200, body="{not json", now=NOW)
    facts = GitHub(store, client=api()).facts(SLUG, now=NOW)

    assert facts.retrieved is False
    assert "corrupt" in facts.error


def test_errors_are_cached_too(store):
    log: list[str] = []
    github = GitHub(store, client=api(repo=None, log=log))

    github.facts(SLUG, now=NOW)
    used = github.requests
    github.facts(SLUG, now=NOW)
    assert github.requests == used


# --- authentication ---------------------------------------------------------


def test_a_token_is_sent_as_a_bearer_header(store):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return (
            httpx.Response(200, json=REPO)
            if str(request.url).endswith(f"/repos/{SLUG}")
            else httpx.Response(404, json={})
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        GitHub(store, token="s3cret", client=client).facts(SLUG, now=NOW)

    assert seen and all(header == "Bearer s3cret" for header in seen)


def test_without_a_token_no_authorization_header_is_sent(store):
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(404, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        github = GitHub(store, token="", client=client)
        github.facts(SLUG, now=NOW)

    assert github.authenticated is False
    assert all(header is None for header in seen)


def test_the_token_is_read_from_the_environment(store, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert GitHub(store).authenticated is True


def test_the_token_never_reaches_the_cache(store):
    github = GitHub(store, token="s3cret", client=api())
    github.facts(SLUG, now=NOW)

    body = store.cached_page(f"github-facts:{SLUG}")["body"]
    assert "s3cret" not in body
    assert json.loads(body)["slug"] == "owner/repo"
