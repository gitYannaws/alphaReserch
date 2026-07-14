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
| 3 | Pain extraction (`claude -p`, verbatim-span required) | `pipeline/extract.py` | done |
| 4 | Embeddings | `pipeline/embed.py` | done |
| 5 | Clustering (themes + distinct-author count) | `pipeline/cluster.py` | done |
| 6 | Demand scoring (intensity, freq, WTP, reachability) | `pipeline/s6_demand.py` | done |
| 7 | Warning flags (advisory: SOC2/HIPAA, marketplace, closed API, regulated) | `pipeline/s7_filters.py` | done |
| 8 | Competitive intel (saturation, persistence, gap) | `pipeline/s8_compete.py` | done |
| 9 | Rank = demand x persistence / saturation | `pipeline/s9_rank.py` | done |
| 10 | Idea generation (top-N only) | `pipeline/s10_ideas.py` | done |
| 11 | Validation plan (one falsifiable kill-test per idea) | `pipeline/s11_validation.py` | done |
| 12 | Report (md, permalinked evidence) | `pipeline/s12_report.py` | done |

GUI wraps stages 1-12.

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
  discover.py                  # pre-stage-1: topic -> firecrawl_search -> validate_candidate
  store.py                     # SQLite: schema + migrations + all read/write
  collectors/
    base.py                    # Collector interface + Document dataclass
    discourse_collector.py     # Discourse JSON (free, structured, per-post author)
    firecrawl_collector.py     # generic forum fallback (Firecrawl REST, thread-level)
    playwright_collector.py    # open JS-rendered pages only; no evasion (infinite-scroll aware)
    fallback_collector.py      # approved firecrawl + playwright auto fallback
    reddit_collector.py        # Reddit via Playwright + old.reddit HTML, per-comment authored (.json is 403-blocked)
  extract.py                   # stage 3 claude/codex, batched, span-validated
  embed.py                     # stage 4 bge embeddings
  cluster.py                   # stage 5 hdbscan themes
  s6_demand.py                 # stage 6 demand score, no competition
  s7_filters.py                # stage 7 advisory warning flags
  s8_compete.py                # stage 8 lightweight competition signals
  s9_rank.py                   # stage 9 rank formula
  s10_ideas.py                 # stage 10 top-N product idea stubs
  s11_validation.py            # stage 11 falsifiable kill-tests
  s12_report.py                # stage 12 Markdown report
webapp/
  app.py                       # FastAPI: /api/run, /status, /docs, /pains, /clusters
  static/index.html            # UI: seed input, live stepper, themes + pains tables
```

## Data model (SQLite)
- `runs(job_id, seed_url, stage, status, created_at)`
- `documents(id, run_id, source_type, source_granularity, source_url, permalink, title, raw_markdown, author_hash, thread_url, created_at, fetched_at)`
- `run_documents(run_id, document_id, collected_at)` links canonical source docs to each run
- `pains(id, run_id, document_id, source_id, author_hash, complaint, workflow_pain, workaround, wish, persona, verbatim_span, span_start, span_end, source_permalink, created_at)`
- `embeddings(pain_id, run_id, vec BLOB)`
- `clusters(id, run_id, label, size, distinct_authors)` + `cluster_members(cluster_id, pain_id)`
- `demand_scores(cluster_id, run_id, pain_intensity, frequency, willingness_to_pay, reachability, demand_score, evidence_count, distinct_authors)`
- `filter_results(cluster_id, run_id, dropped, reasons)`
- `competitive_intel(cluster_id, run_id, incumbent_count, saturation_score, persistence_score, gap_summary)`
- `rankings(cluster_id, run_id, rank, rank_score, demand_score, persistence_score, saturation_score, dropped, filter_reasons)`
- `ideas(id, run_id, cluster_id, title, pitch, evidence_permalink, created_at)`
- `validation_plans(id, run_id, idea_id, kill_test, metric, threshold, timeframe, channel)`
- `reports(run_id, path, created_at)`

## Design rules (do not violate)
- **Source-agnostic collectors.** New source = new `Collector` subclass. Never hardcode one platform.
- **Verbatim-span gate (stage 3).** Every pain must carry an exact source substring. No span or span-not-in-source = dropped. Store span offsets when accepted. No hallucinated evidence.
- **Extractor fallback.** Try Claude first; fall back to Codex only for configured quota/rate-limit markers such as API 429. Codex output still goes through the exact-span gate.
- **Distinct authors, not post count (stage 5).** One loud user is not a market.
- **Source granularity matters.** Discourse is post-level with per-post authors; Firecrawl is thread-level unless it can prove per-post authors. Later scores should discount weak author evidence.
- **Firecrawl is opt-in.** Do not use Firecrawl as an implicit fallback from the website. A run must explicitly approve Firecrawl, or config/CLI must set `collection.fallback` to `firecrawl` or `auto`.
- **Competition is NOT in demand score.** Demand (stage 6) and competition (stage 8) stay separate; they meet only at rank (stage 9).
- **Warning flags are advisory (stage 7).** Flag caveats clearly without hiding the theme.
- **Persistence test is the real signal (stage 8.3).** Complaints post-dating an incumbent's launch = incumbent is bad = opportunity. Saturation is not the inverse of opportunity.
- **Privacy.** Store `author_hash` (salted), never raw usernames. Treat raw markdown and verbatim spans as potentially identifying in exports.

## Access boundaries (learned)
- **Firecrawl** = open forums only. Key in `.env` (gitignored).
- **Reddit** = headless Playwright over **old.reddit.com HTML**, per-comment authored,
  auto-detected by host. The public `.json` API now 403s ("blocked by network security")
  for any non-residential client - even a real browser hitting the .json path - so JSON is
  dead from datacenter IPs; old.reddit HTML still renders (200) and is parsed from the DOM.
  Real browser UA + `over18=1` age-ack cookie, no stealth/proxy/CAPTCHA; stops on
  403/429 and login/bot walls. Needs Playwright chromium installed.

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
`.env` needs `FIRECRAWL_API_KEY`. `claude` CLI must be logged in (used by stage 3).
