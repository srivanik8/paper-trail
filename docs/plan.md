# paper-trail — 7-day build plan

> A daily AI news digest that only surfaces items it can trace to a primary source.

**Not** a fake-news detector. No model verifies truth. This scores *provenance and
signal*: does the claim link to a paper, a repo, or an official lab post — and does
that artifact hold up when you look at it?

---

## Repo name

**`paper-trail`** — the pun works twice (provenance trail + arXiv papers), it's short,
and an interviewer understands the project from the name alone.

Backups if it's taken or you want something else:

| Name | Read |
|---|---|
| `receipts` | "show me the receipts" — memorable, slightly informal |
| `primary-source` | Literal, zero ambiguity, a bit dry |
| `signalcheck` | Emphasises the scoring half over the provenance half |
| `ai-news-provenance` | Boring but unmistakably descriptive |

Tagline for the README: *"AI news, filtered by whether it can show its work."*

---

## Shape of the week

Each day ships something runnable. The vertical slice (one source → dedupe → email)
is done by day 2 so nothing is left integrating on day 7. Budget ~2–3 focused hours
a day; days 3 and 4 are the heavy ones.

```
D1  skeleton + HN ingest          → items printed to stdout
D2  sqlite + dedup                → second run emits nothing new
D3  primary-source resolution     → every item carries an evidence type
D4  repo/paper reality checks     → "is this repo real" returns something you trust
D5  LLM scoring on survivors      → ranked top 10 with hype flags
D6  render + send                 → an email you'd actually read
D7  GitHub Actions + README       → green checkmark every morning
```

---

## Day 1 — Skeleton and one source, end to end

**Goal:** normalized items on stdout from a real API.

- `uv init`, Python 3.12, `ruff` + `pytest`. Don't add a framework you don't need yet.
- Define the schema once, in one place — a `dataclass` or Pydantic model:
  `{id, title, url, source, published_at, raw_signal, primary_source_url}`.
- One ingester: **HN Algolia** (`hn.algolia.com/api/v1/search_by_date?tags=story`) —
  no key, has `points` and `num_comments` for `raw_signal`.
- CLI entry point: `python -m papertrail run --since 24h --dry-run`.

**Done when:** the command prints a table of the last 24h of AI-ish HN stories.

**Trap:** timezones. Store every `published_at` as UTC ISO-8601 on day 1. Fixing this
on day 5 means rewriting your dedup window.

---

## Day 2 — Storage and dedup

**Goal:** running twice in a row produces nothing the second time.

- `sqlite3` from the stdlib — no ORM. Two tables:
  - `items` — everything ever seen, including rejects, with the reason.
  - `sends` — what actually went out, and in which digest.
- **URL canonicalization** before hashing: lowercase host, strip `utm_*`/`ref`/`fbclid`,
  drop trailing slash, unwrap redirectors. `id = sha256(canonical_url)`.
- **Fuzzy title dedup** — URL matching alone will not save you. `rapidfuzz`
  `token_set_ratio >= 88` inside a rolling 7-day window. Cluster matches into one
  story; keep the highest-`raw_signal` member as canonical and retain the rest as
  `also_seen` — that list is genuinely nice in the email ("covered by HN, r/LocalLLaMA,
  Import AI").
- Add ingesters 2 and 3 now that the shape is proven: **HF daily papers**
  (`huggingface.co/api/daily_papers`) and **arXiv** (cs.AI, cs.LG, cs.CL).

**Done when:** `run` twice back-to-back, second run reports `0 new`.

**Trap:** arXiv's API asks for ~3s between requests. Build the delay in now rather
than getting throttled mid-demo.

---

## Day 3 — Primary-source resolution

This is the heart of the project. Everything else is plumbing around it.

**Goal:** every surviving item carries an `evidence` value and a `primary_source_url`.

- Given an item URL, resolve the primary source:
  1. Is the URL *itself* primary? (`arxiv.org/abs/*`, `github.com/{owner}/{repo}`,
     `huggingface.co/{org}/{model}`, an allow-list of lab domains.)
  2. If not, fetch the page once (5s timeout, cached in sqlite) and extract outbound
     links matching those same patterns.
- `evidence` is a closed enum: `paper | repo | model_weights | official_blog | none`.
- Items resolving to `none` are dropped before the LLM ever sees them — that's most
  of your volume and all of your cost saving.

**Done when:** you've hand-checked ~50 real items and can state the precision of the
resolver as a number.

**Trap:** don't scrape aggressively. One fetch per unique URL, cached, `User-Agent`
set honestly, `robots.txt` respected. This is the part that breaks in week 6 if you
get it wrong now.

---

## Day 4 — Reality checks on repos and papers

**Goal:** the "is this repo real?" function, and you trust its answers.

Via the GitHub API (`GITHUB_TOKEN` → 5000 req/hr; cache metadata 24h):

- `created_at`, first vs. last commit date → **history shorter than 7 days**
- contributor count → **single contributor**
- tree API, count non-`.md` files → **README-only**
- stars ÷ days since creation → star velocity (the honest version of "trending")
- absence of a license, presence of a waitlist/Typeform link in the README

For arXiv: does a v2 exist, is it withdrawn, are the author affiliations resolvable.

Emit a `substance_flags` list from a **fixed vocabulary**. Fixed matters — free-text
flags mean you can't aggregate them next month.

**Done when:** it correctly separates 5 repos you know are real from 5 you know are
vapor. Pick them by hand today; they're your regression test forever.

---

## Day 5 — Stage 2: LLM scoring

**Goal:** a ranked top 10 out of the survivors, for a few cents a day.

- Batch 25 items per call. Use structured output / a tool schema so you're never
  parsing markdown fences out of a response.
- **Feed the provenance facts into the prompt.** The model scores *given* the evidence
  you gathered on days 3–4; it does not guess at whether a repo is real. This is the
  single thing that makes the output defensible in an interview.
- `hype_flags` comes from a closed vocabulary listed in the prompt — same reason as
  day 4.
- Log tokens and cost per run to sqlite.
- Log **every rejected item with its score and reason**. After a month that's a
  labelled dataset of your own judgment, and it's the most interesting thing you'll
  have to write up.

**Done when:** you disagree with fewer than ~2 of the top 10 on a day you've read
the news yourself.

---

## Day 6 — Render and deliver

**Goal:** an email in your inbox you'd actually open.

- Jinja2 → HTML, inline CSS, table-based layout (email clients are 2003 forever).
- Grouped by category. Per item: one line of substance, an evidence badge, any hype
  flags, and links to *both* the primary source and the discussion.
- **Resend** free tier — simpler than SMTP with Gmail app passwords.
- `--dry-run` writes `out/digest.html` so you can iterate on the template without
  sending anything.

**Done when:** you've received it, on your phone, and it looked right.

---

## Day 7 — Ship it on Actions, and write it up

**Goal:** it runs without you.

- Workflow: `schedule` cron plus `workflow_dispatch` for manual runs. Cron is UTC —
  convert your morning. Secrets in repo settings.
- **The state problem:** an Actions runner is ephemeral, so your sqlite file dies with
  it. Options: `actions/cache` (can evict — silently breaks dedup), a hosted db
  (Turso/Neon free tier), or commit state back to the repo.
  **Recommended:** commit an append-only `data/items.jsonl` and rebuild sqlite from it
  at the start of each run. It's diffable, it survives, and it makes your labelled
  dataset visible on GitHub — which is the portfolio artifact.
- README: a screenshot of a real digest, the pipeline diagram, and the design
  rationale (*provenance, not truth*). The README is what a recruiter reads.

**Done when:** green checkmark, email arrived, README done.

---

## After week 1

- ~2 weeks of logged scores → tune the rubric against your own judgments.
- Add sources one at a time (lab RSS, r/LocalLLaMA JSON), each behind the same
  normalize step.
- The write-up: "what I learned filtering AI news by provenance for a month" —
  with the dataset attached.

## Cost

Well under $1/month. LLM calls are a few cents a day; everything else is free tier.
