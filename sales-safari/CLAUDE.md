# CLAUDE.md - Sales Safari

Niche pain-mining pipeline. Scrape a community, extract complaints, cluster into themes,
score demand, check competition, rank, generate + validate product ideas. Forum-first.

## Git workflow
**Never create a branch.** Commit and push directly to `master`.

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
    playwright_collector.py    # open JS-rendered pages only; no evasion (infinite-scroll aware); strips site chrome + quotes, dedupes by rel=canonical, and emits POST-level docs when the rendered DOM is XenForo
    xenforo_collector.py       # static XenForo over plain requests (browser UA, no headless); `parse_thread_posts`/`is_xenforo_html`/`base_thread_url` are reused by the renderer
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
  s12_report.py                # stage 12 Markdown report; counts only NON-dropped themes as
                               # "ranked", and prints the live stage-9 formula (no saturation
                               # divisor - stage 9 dropped it 2026-07-20)
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
- **A real span can still be too short to be evidence** (`extract.min_span_chars`, default 25).
  Run `pbfamily0001` stored 2,242 pains whose entire evidence was under 25 characters ("Oh
  Lordy.", "over priced", "your advice is wrong lol"); 1,199 reached clustering and 219 sat
  inside top-100 ranked themes. Too short to embed meaningfully and too short to read.
  Deterministic and free, unlike the stage-3b judge. `scripts/prune_short_spans.py` backfills
  a finished run by marking those pains `verified=0` (reversible, deletes nothing).
- **Recall at 3a, precision at 3b.** Stage 3a casts a wide net on purpose and must NOT self-censor advice/opinion; 3b type-classifies and applies `verify.keep_types`. Turning 3b off without re-tightening 3a leaves the pipeline with no precision gate at all.
- **Keep/drop line is GENUINE PAIN vs NOISE, not product-shaped.** A social gripe is a real pain and can become a product; solvability is judged later at the THEME level (7b + idea gen), never discarded at extraction.
- **Extractor fallback.** Try Claude first; fall back to Codex only for configured quota/rate-limit markers such as API 429. Codex output still goes through the exact-span gate.
- **Distinct authors, not post count (stage 5).** One loud user is not a market.
- **The cohesion guard must sit near the real distribution** (`cluster.min_cohesion`). At
  0.55 it never fired once: across run `pbfamily0001`'s 8,925 themes, measured cohesion runs
  0.855 (5th pct) / 0.908 (median) / 0.954 (95th pct). The one true grab-bag — 580 members,
  493 authors, grouped on the word "she", holding "She fell 25 floors and didn't die
  immediately??" — sat at 0.723 and sailed through. At **0.80** the guard is surgical: 4 of
  3,707 eligible clusters split, and that blob becomes 12 coherent sub-themes of 18-92
  members, all rankable. Do not go past ~0.82 (0.85 splits 287 clusters, 0.88 splits 1,375).
- **Merging near-duplicate themes by centroid does NOT work here — do not retry it.** Measured
  on `pbfamily0001`: at cosine 0.95 it merges only 73 groups and changes nothing; at 0.92 and
  below, single-linkage chaining collapses the space into one blob (7,742 members at 0.92,
  18,537 at 0.90, 31,893 at 0.85). Fragmentation is real, but this is not its cure.
- **A theme's name must be true.** The semantic audit prompt shows its output shape with a
  literal example label, and a model with nothing to say echoes it back. That echo was stored
  as a theme name on `pbfamily0001`. `cluster._PLACEHOLDER_LABELS` rejects it; the GROUP is
  kept and falls back to a representative member quote, so no pain is lost to a bad label.
- **Source granularity matters.** Discourse is post-level with per-post authors; Firecrawl is thread-level unless it can prove per-post authors. Later scores should discount weak author evidence.
- **Render to reach a board, then parse it properly.** A rendered page is not an excuse for a
  thread blob. `playwright_collector` runs the XenForo post parser over the DOM it loaded, so a
  board that gates plain requests behind a JS challenge still yields per-post authors. Rendering
  is where the line sits: the site's own JS runs in a real browser, but no collector may
  reimplement a proof-of-work/CAPTCHA challenge in Python — that is evasion, not collection.
- **Dedupe by `rel=canonical`, not by URL string.** XenForo serves one thread at `/threads/x.9`,
  `/x.9/latest` and `/x.9/page-2`. Run `e81aed813e0f` stored 47 documents for ~25 threads because
  each alias looked new; content-hash dedupe is the second net behind canonical.
- **Walk BOTH paginations, and state the ceiling.** Thread discovery follows the listing's
  `rel="next"` (`collection.max_listing_pages`) and each thread's own pages
  (`collection.max_thread_pages`). Reading only the seed page reaches whatever that page links:
  on `geomax.me/forums/geomax.2` that is 21 threads of ~5,300 across 254 listing pages, i.e.
  0.4% of the board. Depth is the operator's call because cost is
  listing-pages x threads-per-page x pages-per-thread fetches (~29s each on a challenged
  board) -- never quietly collect a slice and report it as the source.
- **A deep backfill must be resumable.** `corpus_mode` `backfill`/`historical` passes the
  corpus's known thread roots to the collector as `skip_thread_urls`, so a runner that dies
  at hour 30 of a 40-hour crawl picks up where it stopped (the resume runner re-enters stage 1
  while progress is short of the cap). `refresh` deliberately passes none -- it must revisit
  known threads to capture posts added since.
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
- **A fallback must be LOUD.** Stages 10a/9b/10b each catch their own LLM failure and carry
  on with a keyword template, which is right — one bad call must not sink a 40-hour run. But
  they reported it with `print()`, and the detached runner captures only its own logger. Run
  `pbfamily0001` therefore finished `status=done` with all 25 ideas as templates, 0
  competitors and 25 unproven briefs; the only clue in the log was `template=25`. Each stage
  now returns `llm_error` (set on an exception AND on an empty answer), `resume.Runner`
  logs `WARN ... DEGRADED`, and a run that ends on any fallback path repeats it as
  `NOTIFY INCOMPLETE`. Never let a stub answer look like work.
- **The guard's liveness check must not read process command lines over stdout.** PowerShell
  runs them through the console formatter, which TRUNCATES at the host buffer width (70 cols
  here) — a ~105-char resume command line arrived as `...\.venv\Scripts\python.exe -`, with
  `pipeline.resume` and the job id cut off, so `process_alive()` said False for a healthy
  runner. `scripts/guard_pipeline.py` then restarted `pbfamily0001` every 10 minutes for six
  hours on 2026-08-21, stacking concurrent runners on one SQLite file: the guard log shows
  RESTARTED at 09:21/09:41/09:51 while extraction progress climbed 176,425 → 177,760 docs
  between them. Stage 5 sat at 2/3 for ~3 hours and stage 7 restarted 10 times, each reset
  zeroing soft-filter progress. Do the matching INSIDE PowerShell and return a COUNT — an
  integer cannot be truncated. An unreadable process check now means ALIVE, never dead.
- **An idea is never a refusal.** Stage 10a returns `{"skip":true,"reason"}` for a theme it
  cannot honestly build a product from, and backfills from further down the ranking. Run
  `9af5b27db46e` shipped 3 of 5 ideas titled "Software-adjacent only: not a product-shaped
  pain".
- **The quote gate tests CONTAINMENT, not equality.** It exists to stop *invented* quotes.
  Testing whole-string equality against the review body enforced a different, much harsher
  rule — quote the ENTIRE review or be discarded. Bodies on `pbfamily0001` ran a median of
  125 chars and 521 at p90, so a model excerpting the damning sentence (exactly what a wedge
  should cite) failed every time: **1 of 21 briefs** carried evidence out of 188 mined
  reviews, and the lone survivor was a 128-char review short enough to echo whole. A
  contiguous substring is still verbatim and still un-inventable, which is the only property
  that matters; `MIN_QUOTE_CHARS = 25` stops a stock phrase matching by chance. The PROMPT
  must agree with the gate — it still said "copied character-for-character" with a wide `or
  ""` escape, and a cautious model answered "" for every incumbent (measured: 0 quotes).
  Saying an excerpt is welcome took it to **7 quotes across 5 briefs**, i.e. 5 of the 6
  briefs that had any reviews to quote. The other 6 have web-only competitors with no App
  Store presence and are honestly unprovable.
- **A wedge without a quote is a hypothesis.** Stage 10b may only cite review text that was
  actually supplied to it; quotes are matched back against the input and dropped if they were
  paraphrased or invented. Briefs with no surviving quote are stored
  `has_review_evidence=0` and labelled *unproven* in the UI — never padded to look researched.
- **Stage 7b must not pay to classify a theme that cannot rank** (`soft_filter.only_rankable`,
  default true). On run `pbfamily0001` the pass spent **186 minutes** on all 8,925 themes at
  ~1.25s each, and only 2,185 could clear rank's `min_support`. Restricting the LLM to that
  rankable quarter costs ~46 minutes and loses **none** of the 856 themes that actually
  ranked (checked against the stored rankings). A skipped theme still gets its row and its
  free regex warnings; its verdict is `not_evaluated` — deliberately distinct from `unknown`
  (asked, no answer) — and stage 9 does not add `not_rankable_solvability` on top of the
  support gap that caused the skip. 7b and 9 share one rule via `s9_rank.cluster_support` /
  `support_reasons`; two copies of that arithmetic would drift and silently lose an
  opportunity rank would have accepted.
- **Warning flags are advisory (stage 7b).** Flag caveats clearly without hiding the theme. They never drop a theme; `min_support` at rank is the only gate.
- **A signal that is computed and then clamped away is not a signal** (`score_full_at`).
  Stage 6 scored linearly under a hard `min(10, ...)`: `frequency = authors * 2.5` hit the
  ceiling at FOUR authors and `recurrence = authors * 1.8` at six. On run `pbfamily0001`
  frequency read exactly 10.00 on 40% of all themes and on effectively every theme that
  ranked, so a 5-author theme and a 23-author theme were indistinguishable and the top of
  the list filled with the smallest themes that could clear `min_support`. The upvote signal
  was added *inside* the same clamp and vanished with it, on a corpus where 177,130
  documents carry a score of up to 2,990. Log curves (`_log_scale`, anchored by
  `score_full_at`) keep rising instead. After: frequency and recurrence pin at 10 on **zero**
  themes and the median size of the top 20 rose from 6 to 9.
- **Recurrence must not be a second copy of frequency.** It used to lead with
  `distinct_authors * 1.8`, so both terms measured the same thing and both pinned at 10.00 on
  every ranked theme. Recurrence is the pain COMING BACK — the same complaint in separate
  threads, workarounds described, phrasing repeated. Author count is frequency's job and is
  not counted twice.
- **Persistence is the real signal, and it now reads real dates.** The keyword proxy gave
  7,775 of `pbfamily0001`'s 8,925 themes the identical 3.0 and the whole run held **4**
  distinct values — a rank multiplier that does nothing — while longer themes contained more
  keywords, so what signal it had tracked cluster size. It is now `span` (months between the
  theme's first and last complaint) plus `recency` (share in the last 12 months), weighted
  6:3 because a burst last month is not persistence. After: **686 distinct values across
  1.0–9.53**. "Now" is the corpus's own newest document (`corpus_latest_date`), never the
  wall clock, so re-scoring a finished run cannot quietly age it. Sources with no dates fall
  back to the keyword proxy.
- **A forum corpus is full of people complaining about the forum** (`platform_meta` +
  `rank.drop_warnings`). An LLM cheerfully calls venue chatter software-solvable:
  `pbfamily0001` ranked "moderator abuse and unfair bans" at 8 and "subreddit toxicity" at
  11; the re-run put "frequent unwanted posts on home page" at 4. Real complaints, wrong
  market. The tag is judged **per member, then by share** (`PLATFORM_META_MIN_SHARE`, 0.5) —
  unlike every other tag, which fires on one hit anywhere in the theme — because this one can
  DROP, so it has to be right about the theme rather than about one post. Warnings stay
  advisory by default; `rank.drop_warnings` is the operator naming the few that disqualify.
  After: venue-meta in the top 100 fell from 10 to 4, and 389 themes were dropped by the tag.
- **Persistence lives in demand, not competition.** Complaints post-dating an incumbent's launch = incumbent is bad = opportunity. It is a property of the pain, scored in demand (stage 6), and is a rank multiplier. NOTE: currently a keyword proxy, not yet the true complaint-date-vs-launch test. Saturation is not the inverse of opportunity.
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
