# paper-trail

AI news, filtered by whether it can show its work.

A daily digest that only surfaces items it can trace back to a primary source.
This is **not** a fake-news detector — no model verifies truth. It scores
**provenance and signal**: can a claim point at a paper, a repo or an official
post, and does that artifact hold up when you look at it?

## Status

Day 1 of 7 — see [`docs/plan.md`](docs/plan.md).

- [x] **D1** Skeleton, common schema, Hacker News ingest
- [ ] **D2** SQLite store, URL canonicalization, fuzzy dedup
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
window: since 2026-08-26T23:31:26Z
4 items (hn 4)

 SIGNAL  PUBLISHED (UTC)   SRC     TITLE
------------------------------------------------------------------------------
  413.0  08-17 20:53       hn      Show HN: Local LLM inference on a Raspberry Pi 5
  298.6  08-17 21:43       hn      Interpretability results for sparse autoencoders
  157.0  08-17 22:16       hn      Ask HN: what agent framework survives production?
  156.1  08-17 22:33       hn      Fine-tuning Llama 4 on a single consumer GPU
```

Options: `--source` to restrict to one feed, `--min-points` for the signal
floor, `--limit` to cap the table, `--json` for newline-delimited JSON.

## Layout

```
src/papertrail/
  models.py      Item — the one schema every source normalizes into
  ids.py         URL canonicalization and stable item ids
  timeutil.py    UTC in, ISO-8601 out; naive datetimes are refused
  relevance.py   cheap keyword pass, tuned for recall
  pipeline.py    fan out to sources, merge, rank
  render.py      terminal table
  cli.py         argparse entry point
  sources/
    base.py      the Source protocol
    hn.py        Hacker News via the Algolia search API
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

## Development

```bash
uv run pytest        # 61 tests, no network
uv run ruff check .
uv run ruff format .
```

Tests use `httpx.MockTransport`, so the suite never touches the network.
