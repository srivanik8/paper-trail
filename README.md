# paper-trail

AI news, filtered by whether it can show its work.

A daily digest that only surfaces items it can trace back to a primary source.
This is **not** a fake-news detector — no model verifies truth. It scores
**provenance and signal**: can a claim point at a paper, a repo or an official
post, and does that artifact hold up when you look at it?

## Status

Day 1 of 7 — see [`docs/plan.md`](docs/plan.md).

- [x] **D1** Skeleton, common schema, Hacker News ingest
- [x] **D2** SQLite store, URL canonicalization, fuzzy dedup
- [x] **D3** Primary-source resolution
- [ ] **D4** Repo and paper reality checks
- [ ] **D5** LLM scoring on the survivors
- [ ] **D6** HTML digest, email delivery
- [ ] **D7** GitHub Actions schedule

## Usage

```bash
uv sync
uv run papertrail run --since 24h --dry-run
```

```
fetched 4 -> 3 stories (3 new, 0 seen before, 0 folded in, 1 unsourced)
evidence: paper 2, repo 1
pages fetched: 2

  SIGNAL  PUBLISHED     SRC     EVID  TITLE
--------------------------------------------------------------------------------
   320.9  01-02 09:00   hn      pape  Sparse autoencoders scale to frontier models
   280.5  01-02 09:00   hn      repo  Show HN: a tiny LLM inference runtime in C
~  190.4  01-02 09:00   hn      pape  We trained a new reasoning model  +hf
```

`~` a previous run already reported this &nbsp;·&nbsp; `EVID` what the story can
be checked against &nbsp;·&nbsp; `+hf` other feeds that carried it.

The 411-point post that isn't there was the highest-scoring item of the run. It
went to `data/` with `reason = "no primary source"`, which is the entire point.

Options: `--source` to restrict to one feed, `--new-only` to hide what you have
already seen, `--db` for the store location, `--dedup-days` and `--threshold` to
tune matching, `--no-fetch` to resolve from URLs alone, `--keep-unsourced` to see
what the resolver misses, `--dry-run` to record nothing, `--json` for
newline-delimited JSON.

### Scoring the classifier

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

Cases live in `data/provenance_cases.jsonl`. They are hand-labelled URL *shapes*
drawn from real-world patterns, so this is a regression guard and a calibration
check — not a field measurement of live pages. Adding a case you expect to fail
is the useful move; that is how the nine bugs below were found.

## Layout

```
src/papertrail/
  models.py      Item — the one schema every source normalizes into
  ids.py         URL canonicalization and stable item ids
  timeutil.py    UTC in, ISO-8601 out; naive datetimes are refused
  relevance.py   cheap keyword pass, tuned for recall
  dedup.py       fuzzy title clustering with a version veto
  provenance.py  what kind of evidence a URL is, if any
  extract.py     candidate links out of a fetched page
  fetcher.py     one polite, cached fetch per URL
  resolver.py    source -> URL -> page, in that order
  audit.py       scoring the classifier against hand labels
  store.py       SQLite: everything seen, including the rejects
  pipeline.py    fan out, cluster, resolve, record
  render.py      terminal table
  cli.py         argparse entry point
  sources/
    base.py         the Source protocol
    hn.py           Hacker News via the Algolia search API
    huggingface.py  daily papers
    arxiv.py        cs.AI, cs.LG, cs.CL submissions
```

## Design notes

**One schema, enforced at the boundary.** `Item` normalizes its own timestamp on
construction and refuses naive datetimes outright, so no later stage has to
guess what timezone a value is in. Adding a source must never widen this shape.

**Identity lives in one function.** `ids.canonical_url` is the only place that
decides what "the same URL" means. Day 2 widens it — stripping tracking params,
lowercasing hosts, unwrapping redirectors — without touching a single call site.

**The keyword filter is tuned for recall, not precision.** A false positive
costs one row in a table; a false negative loses a story forever. Day 3 drops
anything without a resolvable primary source, which clears out the slop.

**A broken feed never takes the run down.** Sources that raise are recorded in
`RunResult.errors` and skipped.

**No similarity threshold separates real duplicates from real distinctions.**
Measured against real headline pairs, the worst genuine rewording scores 92 and
the worst false merge scores 97 — they overlap, so no cut point works. The 97 is
*"GPT-5 benchmark results published"* against *"GPT-4 benchmark results
published"*: one character apart, different stories. In AI news the version
number **is** the story, so it gets a categorical veto rather than a vote. Two
titles whose numeric tokens disagree never merge, whatever they score.

**Rejects are kept, with the reason.** `items` records everything ever seen,
including what was dropped and why. A month of that is a labelled record of what
the rubric decided, and the only way to tune it later.

**A cluster knows more than its best member.** The report that reaches the HN
front page carries a score but no provenance; the arXiv entry for the same work
carries provenance and no score. `Cluster.primary_source_url` reads across every
member, and `Resolver.resolve_cluster` tries members in rank order.

**Resolution is ordered cheapest-first.** Ask the source (Hugging Face and arXiv
hand over the paper for free), then the URL itself (most survivors settle here,
with no network at all), and only then fetch the page. Anything still unresolved
is recorded and dropped before the LLM stage exists to cost money.

**Navigation links are not evidence.** Extraction skips `nav`, `header`, `footer`
and `aside`, and drops links back into the page's own host. Without that, every
article on a site resolves to whatever repository that site links in its chrome.

**The audit set is written to fail.** A case file that only contains what the
classifier already handles scores 100% and measures nothing. Adding eighteen
cases chosen to break it dropped accuracy to 88.5% and exposed four false
positives — every lab's *careers* page was being read as an official
announcement — plus four missing journal hosts and a Hugging Face collection
misread as weights.

## Development

```bash
uv run pytest        # 349 tests, no network
uv run ruff check .
uv run ruff format .
```

Tests use `httpx.MockTransport`, so the suite never touches the network.
