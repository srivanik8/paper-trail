from datetime import UTC, datetime, timedelta

import httpx
import pytest

from papertrail.checker import Checker
from papertrail.github import GitHub
from papertrail.papers import ArxivPapers
from papertrail.provenance import NONE, Evidence, Provenance, classify
from papertrail.store import Store
from papertrail.substance import Flag

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def github(store: Store, code_files: int = 40, contributors: int = 47, readme: str = "cmake"):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/contributors" in url:
            return httpx.Response(
                200,
                json=[{"login": "a"}],
                headers={"link": f'<...&page={contributors}>; rel="last"'},
            )
        if "/commits" in url:
            date = (NOW - timedelta(days=700)).isoformat() if "page=2" in url else NOW.isoformat()
            return httpx.Response(
                200,
                json=[{"commit": {"committer": {"date": date}}}],
                headers={"link": '<...&page=2>; rel="last"'},
            )
        if "/git/trees/" in url:
            tree = [{"type": "blob", "path": f"src/f{i}.py"} for i in range(code_files)]
            return httpx.Response(200, json={"tree": tree})
        if url.endswith("/readme"):
            import base64

            return httpx.Response(
                200,
                json={"encoding": "base64", "content": base64.b64encode(readme.encode()).decode()},
            )
        return httpx.Response(
            200,
            json={
                "full_name": "owner/repo",
                "stargazers_count": 4200,
                "created_at": "2024-01-01T00:00:00Z",
                "pushed_at": NOW.isoformat(),
                "default_branch": "main",
                "license": {"spdx_id": "MIT"},
            },
        )

    return GitHub(store, client=httpx.Client(transport=httpx.MockTransport(handler)))


def papers(store: Store, version: int = 3, authors: int = 6):
    body = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>http://arxiv.org/abs/2401.00001v{version}</id><title>A paper</title>
    <published>2024-01-01T09:00:00Z</published>
    {"".join(f"<author><name>A{i}</name></author>" for i in range(authors))}
    </entry></feed>"""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    return ArxivPapers(store, client=client, delay=0)


def test_a_repository_is_checked_through_github(store):
    checker = Checker(github=github(store))
    result = checker.check(classify("https://github.com/owner/repo"), now=NOW)

    assert result.flags == ()
    assert result.star_velocity is not None
    assert result.notes["slug"] == "owner/repo"


def test_a_thin_repository_is_reported_as_thin(store):
    checker = Checker(github=github(store, code_files=0, contributors=1))
    result = checker.check(classify("https://github.com/owner/repo"), now=NOW)

    assert Flag.README_ONLY in result.flags
    assert result.thin is True


def test_a_deep_repo_link_is_checked_at_the_repository_root(store):
    checker = Checker(github=github(store))
    result = checker.check(classify("https://github.com/owner/repo/blob/main/train.py"), now=NOW)
    assert result.notes["slug"] == "owner/repo"


def test_a_paper_is_checked_through_arxiv(store):
    checker = Checker(papers=papers(store, version=1, authors=1))
    result = checker.check(classify("https://arxiv.org/abs/2401.00001"), now=NOW)

    assert Flag.UNREVISED in result.flags
    assert Flag.SINGLE_AUTHOR in result.flags


def test_unresolved_provenance_is_not_checked(store):
    checker = Checker(github=github(store), papers=papers(store))
    assert checker.check(NONE, now=NOW).flags == ()
    assert checker.requests == 0


@pytest.mark.parametrize(
    "url",
    ["https://huggingface.co/org/model", "https://www.anthropic.com/news/a-post"],
)
def test_evidence_with_nothing_to_check_returns_an_empty_assessment(store, url):
    """Weights and blog posts have no commit history; saying nothing is honest."""
    checker = Checker(github=github(store), papers=papers(store))
    result = checker.check(classify(url), now=NOW)

    assert result.flags == ()
    assert checker.requests == 0


def test_without_a_github_client_repositories_are_skipped(store):
    checker = Checker(papers=papers(store))
    assert checker.check(classify("https://github.com/owner/repo"), now=NOW).flags == ()


def test_without_a_papers_client_papers_are_skipped(store):
    checker = Checker(github=github(store))
    assert checker.check(classify("https://arxiv.org/abs/2401.00001"), now=NOW).flags == ()


def test_a_repo_url_that_yields_no_slug_is_skipped(store):
    checker = Checker(github=github(store))
    fake = Provenance(Evidence.REPO, "https://github.com/trending", via="page")
    assert checker.check(fake, now=NOW).flags == ()
    assert checker.requests == 0


def test_a_paper_url_that_is_not_arxiv_is_skipped(store):
    checker = Checker(papers=papers(store))
    fake = Provenance(Evidence.PAPER, "https://www.nature.com/articles/s41586-1", via="self")
    assert checker.check(fake, now=NOW).flags == ()
    assert checker.requests == 0


def test_requests_are_counted_across_both_gatherers(store):
    checker = Checker(github=github(store), papers=papers(store))
    checker.check(classify("https://github.com/owner/repo"), now=NOW)
    checker.check(classify("https://arxiv.org/abs/2401.00001"), now=NOW)
    assert checker.requests > 1


def test_a_second_check_of_the_same_repo_costs_nothing(store):
    checker = Checker(github=github(store))
    checker.check(classify("https://github.com/owner/repo"), now=NOW)
    spent = checker.requests
    checker.check(classify("https://github.com/owner/repo"), now=NOW)
    assert checker.requests == spent
