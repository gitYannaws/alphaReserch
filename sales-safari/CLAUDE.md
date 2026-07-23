# CLAUDE.md - Sales Safari

Niche pain-mining pipeline. Scrape a community, extract complaints, cluster into themes,
score demand, check competition, rank, generate + validate product ideas. Forum-first.

## Stack
- Python 3.12, SQLite (single file `db/safari.sqlite`)
- Collection: Discourse JSON (plain `requests`) primary; Firecrawl only when explicitly approved
- Optional rendering fallback: Playwright for open JS-rendered forums only
- Pain extraction: `claude -p` primary; Codex CLI fallback for quota/rate-limit failures
- Embeddings: `sentence-transformers` (bge-small default, bge-large optional), local
- Clustering: `hdbscan` + `umap-learn`
- Web: FastAPI + uvicorn + vanilla-JS single page (no build step)

## Pipeline (stages 0-12)
| # | Stage | Module | Status |
|---|-------|--------|--------|
| 0 | Config | `config.yaml` | done |
| 1 | Collect | `pipeline/collectors/*` | done |
| 2 | Store (SQLite) | `pipeline/store.py` | done |
| 3a | Pain extraction — wide net for RECALL, verbatim-span required | `pipeline/extract.py` | done |
| 3b | Verify (type-classify each candidate; `keep_types` policy decides survival) | `pipeline/extract.py` | done |
| 4 | Embeddings | `pipeline/embed.py` | done |
| 5 | Clustering (themes + distinct-author count) | `pipeline/cluster.py` | done |
| 6 | Demand scoring (intensity, freq, WTP, reachability, recurrence, **persistence**) | `pipeline/s6_demand.py` | done |
| 7b | Advisory pass: software fit (LLM) **+ warning tags** (regex) — one row per theme | `pipeline/s7b_softfilter.py` | done |
| 9 | Rank = demand x persistence x solvable-weight (single pass) | `pipeline/s9_rank.py` | done |
| 10a | Idea draft (LLM, top-N ranked themes; unbuildable themes skipped + backfilled) | `pipeline/s10_ideas.py` | done |
| 9b | Competitor discovery — names the real products competing with each **drafted idea** | `pipeline/s9b_competitors.py` | done |
| 9c | Review mining (1-2★ reviews of those competitors) | `pipeline/reviews.py` | done |
| 10b | Idea brief — problem, target user, wedge, incumbent failures, MVP, risks | `pipeline/s10b_brief.py` | done |
| 12 | Report (md, permalinked evidence) | `pipeline/s12_report.py` | done |

**The idea chain runs rank → draft idea → competitors OF that idea → their low-star reviews
→ brief built on the gap those reviews expose** (reordered 2026-07-20). Competitor discovery
used to run *before* rank against raw themes, which failed two ways on run `9af5b27db46e`:
it found competitors for 8 of 642 themes (and named mostly magazines), and none of them
belonged to a theme that became an idea — so the UI, which shows competitors on idea cards,
displayed nothing. Asking about a drafted idea instead of a vague theme covers ~5 items, not
~640, which is what makes a strong model affordable here.

**Persistence lives in demand (stage 6), not competition** — it's a property of the pain
(does it keep recurring), stored in `demand_scores`; rank reads it from there. (The old
keyword `s8_compete.py` is retired from the live pipeline; the file stays on disk for the
standalone `analyze.py`.)

GUI wraps the pipeline as **14 display steps**: collect, store, extract, verify, embed,
cluster, score, software fit + warnings, rank, ideas, competitors, reviews, brief, report.
Run sequence + resume/skip toggles live in `pipeline/resume.py`. 9b/9c/10b toggle via
`competitors.enabled` / `reviews.enabled` / `brief.enabled`; 3b via `verify.enabled`.

**Stage 7 was folded into 7b (2026-07-18).** It was pure regex (free) over the same clusters,
so a separate stage bought nothing; warnings now ride on `soft_filters.warnings`. Neither
half ever drops a theme — `min_support` at rank is the sole gate. `s7_filters.py` remains as
the pattern module (used by `evaluate_cluster` and the standalone `analyze.py`). With `competitors.cover_top: 0` (default) 9b
covers all themes before rank; set it > 0 for legacy cost-bounded top-N (needs an initial rank).

## Topic discovery (pre-stage-1)
Optional front-of-pipeline step: start from a topic/category instead of a URL. Firecrawl
web-searches for candidate communities (`pipeline/discover.py`), then validates each for
*real people posting* before any run:
- **Authored sources** (Reddit/Discourse/XenForo): free probe, count distinct authors -> `legit` (>= `min_authors`), `weak` (1-2), or `reject` (0).
- **Generic thread-level** (Firecrawl): structural probe only (map + thread-pattern filter, no scrape) -> `weak` if forum-shaped (>= `min_threads`), else `reject`. Authorship unverified.
Blogs/SEO/product pages fail the distinct-author bar by design. Firecrawl-gated (opt-in).
Review-then-pick: verdicts shown in the GUI; the user ticks sources, each runs as its own
job through the normal `/api/run`. Endpoints: `POST /api/discover`, `GET /api/discover/{id}`.

## Module map
```
config.yaml                    # all knobs (seeds, keywords, weights, filters, models)
run.py                         # CLI: collect a seed
pipeline/
  orchestrate.py               # load_config + pick_collector (shared by CLI & web)
  resume.py                    # run orchestration: ordered stage sequence + resume/skip toggles
  discover.py                  # pre-stage-1: topic -> firecrawl_search -> validate_candidate
  store.py                     # SQLite: schema + migrations + all read/write
  collectors/
    base.py                    # Collector interface + Document dataclass
    discourse_collector.py     # Discourse JSON (free, structured, per-post author); delta refresh skips topics with no new posts (known_thread_stats)
    firecrawl_collector.py     # generic forum fallback (Firecrawl REST, thread-level)
    playwright_collector.py    # open JS-rendered pages only; no evasion (infinite-scroll aware)
    fallback_collector.py      # approved firecrawl + playwright auto fallback
    reddit_collector.py        # Reddit LIVE via Playwright + old.reddit HTML, per-comment authored (.json is 403-blocked)
    arcticshift_collector.py   # Reddit HISTORICAL via Arctic Shift archive (API or local .zst dumps); full history, scores, zero reddit.com load
  extract.py                   # stage 3a extract (recall) + 3b verify (precision); span-validated
  embed.py                     # stage 4 bge embeddings
  cluster.py                   # stage 5 hdbscan themes
  s6_demand.py                 # stage 6 demand score, no competition
  s7_filters.py                # regex warning patterns (evaluate_cluster); NOT its own stage
  s7b_softfilter.py            # stage 7b advisory pass: solvability (LLM) + warnings (regex)
  s8_compete.py                # RETIRED from live pipeline (kept for analyze.py); persistence moved to s6, saturation to s9b
  s9_rank.py                   # stage 9 rank formula (single pass, no saturation term)
  s10_ideas.py                 # stage 10a draft idea per top theme (ideas.model); skips unbuildable themes
  s9b_competitors.py           # stage 9b competitors OF each drafted idea; strong model + live-URL gate
  reviews.py                   # stage 9c 1-2 star review mining of those competitors
  s10b_brief.py                # stage 10b full build brief; wedge must trace to a supplied review quote
  s12_report.py                # stage 12 Markdown report
webapp/
  app.py                       # FastAPI: /api/run, /status, /docs, /pains, /clusters
  static/index.html            # UI: seed input, live stepper, themes + pains tables
```

## Data model (SQLite)
- `runs(job_id, seed_url, stage, status, created_at, cooldown, thread_limit, ...)` — per-run UI
  toggles are persisted so a detached runner (and any later reconcile respawn) reconstructs
  the run from the DB alone
- `documents(id, run_id, source_type, source_granularity, source_url, permalink, title, raw_markdown, author_hash, thread_url, created_at, fetched_at)`
- `run_documents(run_id, document_id, collected_at)` links canonical source docs to each run
- `pains(id, run_id, document_id, source_id, author_hash, complaint, workflow_pain, workaround, wish, persona, verbatim_span, span_start, span_end, source_permalink, created_at, verified, pain_type, verify_reason)` — stage 3b writes the last three; `verified=0` is withheld by `get_pains` (NULL = never verified, kept)
- `embeddings(pain_id, run_id, vec BLOB)`
- `clusters(id, run_id, label, size, distinct_authors)` + `cluster_members(cluster_id, pain_id)`
- `demand_scores(cluster_id, run_id, pain_intensity, frequency, willingness_to_pay, reachability, recurrence_score, persistence_score, demand_score, evidence_count, distinct_authors, scoring_evidence)` — persistence is a rank multiplier, not in the demand average
- `filter_results(cluster_id, run_id, dropped, reasons)` — LEGACY: no longer written or read by
  the live pipeline (stage 7 folded into 7b); retained for old runs + `analyze.py`
- `soft_filters(cluster_id, run_id, solvable, confidence, reason, warnings)` — stage 7b:
  software-fit score **and** the advisory warning tags (JSON list)
- `competitive_intel(cluster_id, run_id, incumbent_count, saturation_score, persistence_score, gap_summary)`
- `rankings(cluster_id, run_id, rank, rank_score, demand_score, persistence_score, saturation_score, dropped, filter_reasons)`
- `competitors(id, run_id, cluster_id, name, url, category, note, review_domain, weakness, app_name)` — stage 9b; `weakness` = where it falls short for *that idea*, `app_name` = App Store search term for 9c
- `competitor_reviews(id, run_id, competitor_id, app_id, app_name, country, rating, title, body, author, version, source_url)` — stage 9c
- `ideas(id, run_id, cluster_id, title, pitch, evidence_permalink, created_at)` — stage 10a draft
- `idea_briefs(idea_id, run_id, cluster_id, problem, target_user, wedge, incumbents, mvp, risks, has_review_evidence, review_quote_count, created_at)` — stage 10b; `incumbents` is JSON `[{name, fails_at, quote, rating, source_url}]` where `quote` is verbatim from a mined review or empty
- `reports(run_id, path, created_at)` — (`validation_plans` table retained for old runs; stage 11 retired)
- `run_progress(run_id, stage, done, total, unit, updated_at)` — live per-stage progress for the GUI
- Corpus mode (reusable per-seed collections): `corpora(corpus_key, seed_url, created_at, updated_at, backfill_completed_at)`, `corpus_documents(corpus_key, document_id, collected_at)`, `sources(id, url, label, corpus_key, added_at, last_queued_at)`

## Design rules (do not violate)
- **Source-agnostic collectors.** New source = new `Collector` subclass. Never hardcode one platform.
- **Verbatim-span gate (stage 3a).** Every pain must carry an exact source substring. No span or span-not-in-source = dropped. Store span offsets when accepted. No hallucinated evidence. NOTE the gate proves the *quote* is real, not that the doc is a pain — that judgment is stage 3b's.
- **Recall at 3a, precision at 3b.** Stage 3a casts a wide net on purpose and must NOT self-censor advice/opinion; 3b type-classifies and applies `verify.keep_types`. Turning 3b off without re-tightening 3a leaves the pipeline with no precision gate at all.
- **Keep/drop line is GENUINE PAIN vs NOISE, not product-shaped.** A social gripe is a real pain and can become a product; solvability is judged later at the THEME level (7b + idea gen), never discarded at extraction.
- **Extractor fallback.** Try Claude first; fall back to Codex only for configured quota/rate-limit markers such as API 429. Codex output still goes through the exact-span gate.
- **Distinct authors, not post count (stage 5).** One loud user is not a market.
- **Source granularity matters.** Discourse is post-level with per-post authors; Firecrawl is thread-level unless it can prove per-post authors. Later scores should discount weak author evidence.
- **Firecrawl is opt-in.** Do not use Firecrawl as an implicit fallback from the website. A run must explicitly approve Firecrawl, or config/CLI must set `collection.fallback` to `firecrawl` or `auto`.
- **Competition is NOT in demand score, and NOT in rank either.** Demand (stage 6) and
  competition (stage 9b) stay separate. Saturation used to divide rank as `/(1+saturation)`;
  that was removed 2026-07-20 because it *rewarded ignorance* — the only themes carrying a
  penalty were the 8 (of 642) the model had actually managed to find competitors for, which
  landed at ranks 151 and 209-213 of 213, while 615 themes it knew nothing about ranked as if
  the field were empty. Competitors are now **evidence for the brief, not a penalty on the
  score**. Saturation is still recorded on `competitive_intel` for display.
- **Competitors must be real products, and must prove it.** Stage 9b names them from world
  knowledge, so every candidate's URL is HTTP-checked before it is stored, and news /
  advocacy / nonprofit / blog categories are rejected outright. Run `9af5b27db46e` had stored
  The Atlantic, VICE and Snopes as competitors and then mined 75 App Store reviews of them as
  "incumbent gaps".
- **An idea is never a refusal.** Stage 10a returns `{"skip":true,"reason"}` for a theme it
  cannot honestly build a product from, and backfills from further down the ranking. Run
  `9af5b27db46e` shipped 3 of 5 ideas titled "Software-adjacent only: not a product-shaped
  pain".
- **A wedge without a quote is a hypothesis.** Stage 10b may only cite review text that was
  actually supplied to it; quotes are matched back against the input and dropped if they were
  paraphrased or invented. Briefs with no surviving quote are stored
  `has_review_evidence=0` and labelled *unproven* in the UI — never padded to look researched.
- **Warning flags are advisory (stage 7b).** Flag caveats clearly without hiding the theme. They never drop a theme; `min_support` at rank is the only gate.
- **Persistence is the real signal.** Complaints post-dating an incumbent's launch = incumbent is bad = opportunity. It is a property of the pain, scored in demand (stage 6), and is a rank multiplier. NOTE: currently a keyword proxy, not yet the true complaint-date-vs-launch test. Saturation is not the inverse of opportunity.
- **Privacy.** Store `author_hash` (salted), never raw usernames. Treat raw markdown and verbatim spans as potentially identifying in exports.

## Access boundaries (learned)
- **Firecrawl** = open forums only. Key in `.env` (gitignored).
- **Reddit** = headless Playwright over **old.reddit.com HTML**, per-comment authored,
  auto-detected by host. The public `.json` API now 403s ("blocked by network security")
  for any non-residential client - even a real browser hitting the .json path - so JSON is
  dead from datacenter IPs; old.reddit HTML still renders (200) and is parsed from the DOM.
  Real browser UA + `over18=1` age-ack cookie, no stealth/proxy/CAPTCHA; stops on
  403/429 and login/bot walls. Needs Playwright chromium installed.
- **Reddit HISTORICAL** = Arctic Shift archive (Pushshift successor) when
  `collection.arctic_shift.enabled` and the run's "historical" toggle is on: full subreddit
  history via their free API (ascending created_utc cursor; first page must OMIT `after` -
  the live API 400s on `after=0`) or local `.zst`/NDJSON dumps in `dump_dir` (zstd
  `--long=31`, reader needs `max_window_size=2**31`). Exact timestamps, per-item score
  (feeds stage-6 upvotes), zero reddit.com requests, no ~1000-item listing cap. Refresh /
  backfill runs keep the live crawl - the archive trails the fresh edge. Gray zone stated
  plainly: public-content archive, research-standard, never blessed by Reddit; preserves
  since-deleted content, so the salted author_hash rule matters extra.

## Run
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows

# CLI collect
.venv/Scripts/python run.py <forum-url> --limit 12
# CLI extract
.venv/Scripts/python -m pipeline.extract <run_id>

# Web app
.venv/Scripts/python -m uvicorn webapp.app:app --port 8000   # http://localhost:8000
```
`.env` needs `FIRECRAWL_API_KEY`. `claude` CLI must be logged in (used by stages 3a/3b).

## Execution model
`POST /api/run` persists the full run row, then drives collection from a **detached
`pipeline.resume` subprocess** — the same runner the startup reconcile uses — so a run
survives the HTTP server being killed or reaped. Consequences to know:
- No `JOBS` entry for such runs, so `/api/status` and `/api/run/{id}/stop` fall through to
  their persisted-state / stopfile branches (status reports `persisted_only: true`).
- `_reconcile_orphaned_runs` on startup respawns anything left in an active status whose
  runner isn't alive (pidfile `db/resume-{job}.pid`).
- Kickoff (`/api/sources/kickoff`) and merge-analyze still run in-process threads. Kickoff
  runs **per-domain lanes** (2026-07-20): sources on different domains collect concurrently
  (`kickoff.max_lanes`, default 3), sources on the same domain stay strictly sequential in
  one lane — reddit.com sees the identical request pattern as the old serial queue. Lane key
  strips `www./old./new./m.` so mirrors share a lane.
- **`webapp/static/index.html` is read into a module constant at import** — edits to the
  UI need a server restart; a browser refresh alone silently serves the old page.
