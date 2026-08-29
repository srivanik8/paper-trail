# paper-trail

AI news, filtered by whether it can show its work.

## About

There is more AI news every morning than anyone can read, and the loudest items
are reliably not the most substantial. paper-trail pulls from a handful of feeds
and keeps only the stories it can trace back to something you could go and check
for yourself: a paper, a repository, published model weights, or a post from the
lab that actually did the work.

It is **not** a fake-news detector. No model verifies truth, and pretending
otherwise is where projects like this fall over. What it does is narrower and
actually computable — it scores **provenance**: can this claim point at a real
artifact, and did anyone independent carry the same story?

That distinction has teeth. On a typical run the highest-scoring item of the day
gets thrown away — a widely-upvoted opinion piece with nothing behind it but the
author's confidence — while a quieter announcement survives because its blog
post links out to an arXiv paper. Everything discarded is kept in the database
with the reason, so "why didn't I see this?" always has an answer.

Right now it ingests, deduplicates, resolves provenance and prints a ranked
table. Scoring the survivors with an LLM, rendering an HTML digest and mailing
it on a schedule are the parts still to come.

## Quick start

```bash
git clone https://github.com/srivanik8/paper-trail
cd paper-trail
uv sync
uv run papertrail run --since 24h
```

That fetches the last 24 hours from Hacker News, Hugging Face daily papers and
arXiv, collapses duplicates, resolves what it can, and prints what survived:

```
fetched 47 -> 12 stories (9 new, 3 seen before, 6 folded in, 29 unsourced)
evidence: paper 5, repo 4, official_blog 2, model_weights 1
pages fetched: 8

  SIGNAL  PUBLISHED     SRC     EVID  TITLE
--------------------------------------------------------------------------------
   320.9  01-02 09:00   hn      pape  Sparse autoencoders scale to frontier models
   280.5  01-02 09:00   hn      repo  Show HN: a tiny LLM inference runtime in C
~  190.4  01-02 09:00   hn      pape  We trained a new reasoning model  +hf
```

Reading a row: `~` means an earlier run already reported this story, `EVID` says
what it can be checked against, and `+hf` names the other feeds that carried it.

State lives in `papertrail.db` next to you. Run it twice and the second run
reports nothing new — that is the point of the database.

## What's in the repo

```
src/papertrail/
  models.py      Item — the one schema every source normalizes into
  timeutil.py    UTC in, ISO-8601 out; naive datetimes are refused
  ids.py         URL canonicalization and stable item ids
  relevance.py   cheap keyword pass over titles, tuned for recall
  dedup.py       fuzzy title clustering with a version veto
  provenance.py  what kind of evidence a URL is, if any
  fetcher.py     one polite, cached fetch per URL
  extract.py     candidate links out of a fetched page
  resolver.py    source -> URL -> page, in that order
  audit.py       scoring the classifier against hand labels
  store.py       SQLite: everything ever seen, including the rejects
  pipeline.py    fan out, cluster, resolve, record
  render.py      the terminal table
  cli.py         argparse entry point
  sources/
    base.py         the Source protocol every ingester implements
    hn.py           Hacker News via the Algolia search API
    huggingface.py  daily papers
    arxiv.py        cs.AI, cs.LG, cs.CL submissions

data/
  provenance_cases.jsonl   hand-labelled URLs the classifier is scored against

tests/            349 tests, none of which touch the network
docs/plan.md      the build plan this project is following
```

## How to run it

The main command is `run`:

```bash
uv run papertrail run --since 24h          # the default
uv run papertrail run --since 7d --limit 20
uv run papertrail run --source hn          # one feed only; repeatable
uv run papertrail run --json               # newline-delimited JSON
```

Useful flags:

| Flag | What it does |
|---|---|
| `--since` | Lookback window: `24h`, `90m`, `7d`, `2w` |
| `--new-only` | Hide stories an earlier run already reported |
| `--keep-unsourced` | Keep stories with no primary source, to see what the resolver is missing |
| `--no-fetch` | Resolve from URLs alone and never read a page — safe with no network |
| `--dry-run` | Read the database to deduplicate, but write nothing back |
| `--db` | Where the SQLite file lives (default `papertrail.db`) |
| `--dedup-days` | How far back to look for stories already handled (default 7) |
| `--threshold` | Title similarity required to merge two items (default 90) |
| `--min-points` | Signal floor on Hacker News (default 5) |

There is also an `audit` command, which scores the provenance classifier against
the hand-labelled URLs in `data/`:

```bash
uv run papertrail audit
```

```
78 cases, 78 correct
accuracy             100.0%
resolution precision 100.0%   (of everything promoted above 'none')

EVIDENCE         PRECISION   RECALL    N
----------------------------------------
paper               100.0%   100.0%   16
repo                100.0%   100.0%    9
model_weights       100.0%   100.0%    4
official_blog       100.0%   100.0%   12
none                100.0%   100.0%   37
```

Those cases are hand-labelled URL *shapes*, so this is a regression guard rather
than a field measurement of live pages. The useful way to extend it is to add a
case you expect to fail — that is how most of the classifier's bugs were found.

To work on the code:

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

## How it works

Four stages, each cheaper than the one after it.

**Ingest.** Each source turns a remote API into the same `Item` shape:
`{title, url, source, published_at, raw_signal, primary_source_url}`. Sources
handle their own pagination and rate limiting, and normalize timestamps to UTC
before returning. `Item` refuses a naive datetime outright rather than guessing
a timezone, so nothing downstream has to wonder. A source that fails is recorded
and skipped — one broken feed never takes the run down.

**Deduplicate.** The same launch shows up on three feeds under three headlines
pointing at three URLs. URL identity is handled first, by canonicalization:
lowercase the host, strip tracking parameters, drop the fragment, fold every
arXiv URL for one paper onto its abstract page. Then titles are compared fuzzily
so that "Mistral releases Large 3" and "Mistral has released Large 3, its new
flagship model" collapse into one story with the highest-signal report as its
canonical member.

That comparison needed more care than a similarity threshold. Measured against
real headline pairs, the worst genuine rewording scores 92 and the worst *false*
merge scores 97 — they overlap, so no cut point separates them. The 97 is
"GPT-5 benchmark results published" against "GPT-4 benchmark results published":
one character apart, different stories. In AI news the version number *is* the
story, so it gets a categorical veto instead of a vote. Two headlines whose
numbers disagree never merge, whatever they score.

**Resolve.** For each surviving story, find the primary source, cheapest route
first:

1. **Ask the source.** Hugging Face and arXiv hand over the paper for free.
2. **Ask the URL.** A link straight to arXiv, GitHub or a lab post needs no
   network at all. Most survivors settle here.
3. **Read the page.** Only now is anything fetched, and only its outbound links
   are examined.

Anything still unresolved is recorded with `reason = "no primary source"` and
dropped. That is most of the volume, removed before any of it costs money.

Link extraction skips `nav`, `header`, `footer` and `aside`, and ignores links
back into the page's own host. Without that, every article on a site resolves to
whatever repository the site happens to link in its chrome.

The fetcher is written to be defensible, because it reads other people's
websites: one fetch per URL ever with failures cached too, so a 404 is not
retried daily; robots.txt honoured and cached per host; an honest User-Agent; a
hard cap on body size; anything that isn't markup rejected before it is read.

**Record.** Everything goes into SQLite — survivors, duplicates and rejects
alike, each with its status and reason. Keeping the rejects is deliberate. After
a month it is a labelled record of what the filter decided, which is the only
way to tune the rubric later, and it is the more interesting half of the dataset.
