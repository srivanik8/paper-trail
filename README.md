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
- [ ] **D3** Primary-source resolution
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
fetched 4 -> 2 stories (2 new, 0 seen before, 2 folded in) [hn 2]

   SIGNAL  PUBLISHED (UTC)   SRC     TITLE
------------------------------------------------------------------------------
 .  320.9  01-02 09:00       hn      Sparse autoencoders scale to frontier models  +hf,arxiv
    157.0  01-02 22:16       hn      Ask HN: what agent framework survives production?
~   156.1  01-02 22:33       hn      Fine-tuning Llama 4 on a single consumer GPU
```

`~` a previous run already reported this &nbsp;·&nbsp; `.` its primary source is
already known &nbsp;·&nbsp; `+hf,arxiv` other feeds that carried it.

Options: `--source` to restrict to one feed, `--new-only` to hide what you have
already seen, `--db` for the store location, `--dedup-days` and `--threshold` to
tune matching, `--dry-run` to record nothing, `--json` for newline-delimited JSON.

## Layout

```
src/papertrail/
  models.py      Item — the one schema every source normalizes into
  ids.py         URL canonicalization and stable item ids
  timeutil.py    UTC in, ISO-8601 out; naive datetimes are refused
  relevance.py   cheap keyword pass, tuned for recall
  dedup.py       fuzzy title clustering with a version veto
  store.py       SQLite: everything seen, including the rejects
  pipeline.py    fan out, cluster, record
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
member.

## Development

```bash
uv run pytest        # 200 tests, no network
uv run ruff check .
uv run ruff format .
```

Tests use `httpx.MockTransport`, so the suite never touches the network.
