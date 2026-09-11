# paper-trail

AI news, filtered by whether it can show its work.

## About

Too much AI news every morning, and the loudest posts are usually the emptiest.

paper-trail reads a few feeds and throws away anything it can't trace back to
something real — a paper, a code repository, model weights, or a post from the
lab that actually did the work. Then it looks at that thing: does the repo have
code in it, or just a README and a waitlist link? Finally a model ranks whatever
survived, and you get the best handful by email.

It can't tell you what's true. Nothing can. It can tell you what shows its work.

A typical morning: the day's most-upvoted post gets dropped because its
"open-source framework" is an empty repo, while a quieter announcement stays
because it links to a real paper. Everything dropped is kept with the reason,
so "why didn't I see this?" always has an answer.

How it works, in five steps:

| | |
|---|---|
| **Ingest** | Hacker News, Hugging Face daily papers, arXiv |
| **Deduplicate** | one story per launch, however many feeds carried it |
| **Resolve** | find the paper or repo behind it — drop what has none |
| **Check** | is that artifact real? commits, contributors, actual code |
| **Score** | a model ranks what's left, given all of the above |

## Quick start

```bash
git clone https://github.com/srivanik8/paper-trail
cd paper-trail
uv sync
uv run papertrail run --since 24h --no-score
```

That fetches the last 24 hours, collapses duplicates, resolves what it can, and
prints what survived. `--no-score` keeps it free; drop it once you have an
`ANTHROPIC_API_KEY` set.

```
fetched 47 -> 12 stories (9 new, 3 seen before, 6 folded in, 29 unsourced)
evidence: paper 5, repo 4, official_blog 2, model_weights 1
flags: readme_only 1, single_contributor 2, waitlist 1
thin: 1 of 12
scored: 1 request, 4080 in, 1400 out, ~$0.0566

  SC   SIGNAL  PUBLISHED     SRC     EVID  TITLE
--------------------------------------------------------------------------------
   8    180.4  06-01 09:00   hn      pape  Sparse autoencoders scale to frontier models
      Sparse autoencoders trained to 34M features on a production model.
   7    240.7  06-01 09:00   hn      repo  Show HN: a tiny LLM inference runtime in C
      A 4k-line inference runtime in C with no dependencies, 180 contributors.
 ! 1    913.0  06-01 09:00   hn      repo  AgentOS: the last agent framework you need
      A README and a waitlist link; no implementation in the repository.
```

`SC` is the model's 0-10 judgement, `~` marks a story an earlier run already
reported, `!` marks one whose artifact looks like a launch page, and `EVID` says
what it can be checked against.

Those three rows are the whole idea. The **bottom** one is the most popular item
of the day by a wide margin — 913 points, 9,100 stars — and its repository is a
README with a waitlist link. Ranking by popularity prints this list upside down.

State lives in `papertrail.db` beside you. Run it twice and the second run
reports nothing new — that is what the database is for.

## How to run it

### Day to day

```bash
uv run papertrail run --since 24h            # the ranked table
uv run papertrail digest                     # render to out/digest.html
uv run papertrail digest --send --to me@example.com
uv run papertrail stats --days 30            # what the filter has decided
uv run papertrail audit                      # score the rules against hand labels
```

`run` and `digest` share the pipeline flags: `--since` (`24h`, `90m`, `7d`),
`--db`, `--model`, and three switches that turn off a stage — `--no-fetch`
(never read a page), `--no-check` (skip the repository and paper lookups) and
`--no-score` (skip the model, and the bill). `run` adds `--source`, `--limit`,
`--json`, `--new-only`, `--keep-unsourced` and `--dry-run`; `digest` adds
`--out`, `--send`, `--to`, `--min-score`, `--again` and `--empty-ok`.

### Every morning, from GitHub Actions

`.github/workflows/digest.yml` runs at 06:30 UTC and on demand. It needs three
repository secrets — `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `PAPERTRAIL_TO` —
plus `PAPERTRAIL_FROM` if you have verified a sending domain. `GITHUB_TOKEN` is
provided automatically and lifts the GitHub API budget from 60 requests an hour
to 5,000.

An Actions runner is destroyed when the job ends, so `papertrail.db` does not
survive to the next morning — and without it, deduplication forgets everything
and the same story arrives every day. `actions/cache` is the obvious fix and the
wrong one: entries are evicted without warning and the failure is silent. So the
state is committed to the repository as JSONL and the database is rebuilt from
it each run:

```
restore  →  run  →  digest  →  send  →  export  →  commit state/
```

```bash
uv run papertrail export     # write state/*.jsonl
uv run papertrail restore    # rebuild the database from them
```

The export is sorted by first sighting, so a scheduled run reads as added lines
in a diff rather than a rewritten file.

### Working on it

```bash
uv run pytest                                # 671 tests, no network
uv run ruff check . && uv run ruff format .
uv run papertrail audit --min-accuracy 1.0   # what CI gates on
```

The suite mocks every outbound call, so it needs no credentials and cannot be
broken by a third-party API having a bad morning. CI runs the same commands, so
a regression in either rule set fails the build rather than being noticed a
month later.

## What's in the repo

```
src/papertrail/
  models.py      Item — the one schema every source normalizes into
  timeutil.py    UTC in, ISO-8601 out; naive datetimes are refused
  ids.py         URL canonicalization and stable item ids
  failure.py     one bounded line per recorded failure
  relevance.py   cheap keyword pass over titles, tuned for recall
  dedup.py       fuzzy title clustering with a version veto
  provenance.py  what kind of evidence a URL is, if any
  fetcher.py     one polite, cached fetch per URL
  extract.py     candidate links out of a fetched page
  resolver.py    source -> URL -> page, in that order
  substance.py   the rules: does the artifact hold up?
  github.py      repository facts from the GitHub API
  papers.py      paper facts from the arXiv API
  checker.py     dispatches on evidence type to the right gatherer
  scoring.py     the rubric and the response schema
  scorer.py      batched model calls, caching, and what they cost
  digest.py      selection, and email-safe HTML and text
  mailer.py      one HTTP call to Resend
  archive.py     JSONL export and rebuild, so state survives a runner
  stats.py       reading back what the filter decided
  audit.py       scoring both rule sets against hand labels
  store.py       SQLite: everything ever seen, including the rejects
  pipeline.py    fan out, cluster, resolve, check, score, record
  render.py      the terminal table
  cli.py         argparse entry point
  sources/
    base.py         the Source protocol every ingester implements
    hn.py           Hacker News via the Algolia search API
    huggingface.py  daily papers
    arxiv.py        cs.AI, cs.LG, cs.CL submissions

data/     hand-labelled cases the rules are scored against, never machine-written
state/    the JSONL archive a scheduled run commits
docs/     the build plan, and a rendered sample digest
tests/    671 tests, none of which touch the network

.github/workflows/
  digest.yml   the 06:30 UTC run
  ci.yml       lint, tests and the rule audit on every push
```
