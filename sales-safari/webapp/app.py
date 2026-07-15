"""Sales Safari web app. Wraps the pipeline with live progress.

Run: .venv/Scripts/python -m uvicorn webapp.app:app --port 8000
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from pipeline.collectors.reddit_collector import RedditCollector
from pipeline.discover import discover_reddit_thread_urls
from pipeline.orchestrate import corpus_key_for_seed, load_config, normalize_seed, pick_collector
from pipeline.retry import run_with_retry
from pipeline.store import Store

load_dotenv()

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
CFG = load_config(str(ROOT / "config.yaml"))
DB = str(ROOT / CFG.get("db_path", "db/safari.sqlite"))
INDEX = (BASE / "static" / "index.html").read_text(encoding="utf-8")
SOURCES = (BASE / "static" / "sources.html").read_text(encoding="utf-8")

_RETRY = CFG.get("retry", {})
RETRY_ATTEMPTS = _RETRY.get("attempts", 3)
RETRY_BASE_DELAY = _RETRY.get("base_delay", 5.0)

app = FastAPI(title="Sales Safari")

JOBS: dict = {}  # job_id -> live progress dict
DISCOVERIES: dict = {}  # discover_id -> live topic-discovery state
KICKOFFS: dict = {}  # kickoff_id -> live sequential-queue progress
KICKOFF_MIN, KICKOFF_MAX = 3, 15
ACTIVE_RUN_STATUSES = {"starting", "collecting", "collecting-paused", "storing",
                       "extracting", "personas", "embedding",
                       "clustering", "scoring", "filtering", "soft-filtering", "competing",
                       "competitor-discovery", "review-mining", "ranking", "ideating",
                       "validating", "reporting", "stopping", "recovering"}


class RunCancelled(Exception):
    pass


class CollectionStalled(RuntimeError):
    pass


class RunReq(BaseModel):
    seed_url: str
    limit: Optional[int] = None
    collector: Optional[str] = None  # "render" | "firecrawl" | "auto"; overrides the two flags
    use_render: bool = False       # auto-pick renderer: XenForo if detected, else Playwright
    use_firecrawl: bool = False
    use_corpus: bool = False
    historical: bool = False      # corpus mode: favor older unseen threads, not refresh
    search_assist: bool = False   # Firecrawl web-search extra Reddit thread URLs before listing walk
    cooldown: bool = True          # Reddit backfill rest breaks (45m work / 30m pause); off = faster, riskier
    extractor: Optional[str] = None  # None/"claude" | "qwen" | "qwen3" | "glm" -> stage-3 provider


def _collector_to_toggles(collector: Optional[str]) -> tuple[bool, bool]:
    """Single GUI 'Collector' choice -> the (use_render, use_firecrawl) pair that yields
    the matching fallback in _run_config. One choice = one collector, no ambiguous combos:
      render    -> render   (auto XenForo/Playwright, no Firecrawl)
      firecrawl -> firecrawl (Firecrawl only)
      auto      -> auto      (Firecrawl + Playwright chain; picking it = approving Firecrawl)
    Unknown/None defaults to render."""
    return {
        "render": (True, False),
        "firecrawl": (False, True),
        "auto": (True, True),
    }.get((collector or "").lower(), (True, False))


def _extractor_providers(name):
    """Map the GUI extractor choice to a provider try-list. Local models fall back to
    Claude if the local endpoint is down. None -> keep config default."""
    if name in ("qwen", "qwen3", "glm", "local"):
        return [name, "claude"]
    if name == "claude":
        return ["claude", "codex"]
    return None


def _extract_provenance(run_cfg: dict, extractor: Optional[str]) -> dict:
    providers = _extractor_providers(extractor) or (run_cfg.get("extract", {}).get("providers") or [])
    provider = providers[0] if providers else (extractor or "")
    provider_cfg = dict(run_cfg.get("extract", {}).get(provider, {}) or {})
    frozen = {k: v for k, v in provider_cfg.items() if k != "api_key"}
    return {
        "extract_provider": provider,
        "extract_model": provider_cfg.get("model") or run_cfg.get("extract", {}).get("claude_model"),
        "extract_base_url": provider_cfg.get("base_url"),
        "extract_config_json": json.dumps(frozen, sort_keys=True),
        "prompt_version": "extract-v1",
    }


class DiscoverReq(BaseModel):
    topic: str
    limit: Optional[int] = None
    use_firecrawl: bool = False


class SourceReq(BaseModel):
    url: str
    label: Optional[str] = None


class KickoffReq(BaseModel):
    source_ids: list[str]
    mode: str = "collect"          # "collect" = stages 1-2 (corpus); "analyze" = stages 3-15
    collector: Optional[str] = None  # "render" | "firecrawl" | "auto"; overrides the two flags
    use_render: bool = False
    use_firecrawl: bool = False
    use_corpus: bool = True
    historical: bool = False
    search_assist: bool = False
    cooldown: bool = True          # Reddit backfill rest breaks; off = faster, riskier
    extractor: Optional[str] = None


class MergeAnalyzeReq(BaseModel):
    source_ids: list[str]
    extractor: Optional[str] = None
    label: Optional[str] = None


def _check_cancelled(job: dict):
    if job.get("stop_requested"):
        raise RunCancelled(job.get("stop_reason") or "Stopped by user")


def _is_collect_pause_worthy(error: Exception) -> bool:
    msg = str(error).lower()
    return ("http 429" in msg or "http 403" in msg or "bot/login wall" in msg
            or "access denied" in msg or "too many requests" in msg)


def _stage(job: dict, name: str, fn, advisory: bool = False, default=None):
    """Run one pipeline stage with retry+backoff. Cancellation is never retried;
    a non-advisory stage that exhausts retries raises (caught by _worker -> fail_run);
    an advisory one returns `default` and the run continues. Wrap only the stage
    *work* here -- keep validation guards (0 posts / 0 pains) at the call site so a
    deterministic empty result is not re-run."""
    return run_with_retry(
        fn, name=name, attempts=RETRY_ATTEMPTS, base_delay=RETRY_BASE_DELAY,
        should_stop=lambda: _check_cancelled(job), cancel_exc=RunCancelled,
        advisory=advisory, default=default,
        log=lambda kind, msg: print(f"[{kind}] {job['job_id']} {msg}", flush=True))


def _run_config(seed: str, use_render: bool, use_firecrawl: bool) -> dict:
    """Map UI toggles to a collection fallback. `use_render` auto-picks the open-forum
    renderer (XenForo if detected, else Playwright); with Firecrawl it becomes the
    firecrawl->playwright auto chain (still XenForo-first)."""
    cfg = dict(CFG)
    cfg["collection"] = dict(CFG.get("collection", {}))
    cfg["collection"]["playwright"] = dict(cfg["collection"].get("playwright", {}))
    if use_firecrawl and use_render:
        cfg["collection"]["fallback"] = "auto"
    elif use_render:
        cfg["collection"]["fallback"] = "render"
    elif use_firecrawl:
        cfg["collection"]["fallback"] = "firecrawl"
    else:
        cfg["collection"]["fallback"] = "none"
    if use_render:
        host = urlparse(seed).netloc.lower()
        allowed = set(cfg["collection"]["playwright"].get("allowed_domains", []))
        if host:
            allowed.add(host)
        cfg["collection"]["playwright"]["allowed_domains"] = sorted(allowed)
    return cfg


def _analyze_stages(job: dict, job_id: str, store, run_cfg: dict):
    """Stages 3-12: extract -> report. Shared by full runs (collect then analyze) and
    analyze-only corpus runs. Assumes run_documents for job_id is already populated -- by
    live collection (_worker) or by link_run_to_corpus (_analyze_worker)."""
    def _set_stage_progress(stage: int, done: int, total: int, unit: str = ""):
        store.set_progress(job_id, stage, done, total, unit)

    # Stage 3: extract pains.
    job.update(status="extracting", stage=3)
    store.set_stage(job_id, 3, "extracting")
    _set_stage_progress(3, 0, 0, "")
    from pipeline.extract import extract_run

    def _extract_progress(d, t, k):
        _check_cancelled(job)
        job.update(pains=k, extract_done=d, extract_total=t)
        store.set_progress(job_id, 3, d, t, "docs")

    _stage(job, "stage 3 extract", lambda: extract_run(
        store, job_id,
        batch_size=run_cfg.get("extract", {}).get("batch_size", 6),
        progress=_extract_progress,
        extract_cfg=run_cfg.get("extract", {}),
        should_stop=lambda: _check_cancelled(job)))
    pain_count = store.count_pains(job_id)
    job.update(pains=pain_count)
    if pain_count == 0:
        raise RuntimeError(
            f"Pain extraction produced 0 pains from {store.count_documents(job_id)} "
            f"saved posts; stopping before report."
        )

    # Stage 3.5: persona canonicalization (advisory) — consolidate free-text personas
    # into a small reusable segment set for filtering.
    if run_cfg.get("personas", {}).get("enabled", True):
        _check_cancelled(job)
        job.update(status="personas")
        _set_stage_progress(3, 0, 0, "segments")
        from pipeline.s3b_personas import personas_run
        pr = _stage(job, "stage 3.5 personas", lambda: personas_run(
            store, job_id,
            max_segments=run_cfg.get("personas", {}).get("max_segments", 12),
            extract_cfg=run_cfg.get("extract", {})), advisory=True)
        if pr is not None:
            job.update(persona_segments=pr.get("segments"))
            _set_stage_progress(3, 1, 1, "segments")

    # Stage 4: embed.
    job.update(status="embedding", stage=4)
    store.set_stage(job_id, 4, "embedding")
    _set_stage_progress(4, 0, 0, "")
    from pipeline.embed import embed_run
    _stage(job, "stage 4 embed",
           lambda: embed_run(store, job_id,
                             run_cfg.get("embed_model", "BAAI/bge-small-en-v1.5"),
                             progress=lambda d, t: _set_stage_progress(4, d, t, "pains")))

    # Stage 5: cluster.
    job.update(status="clustering", stage=5)
    store.set_stage(job_id, 5, "clustering")
    _set_stage_progress(5, 0, 0, "")
    from pipeline.cluster import cluster_run
    _cl_cfg = run_cfg.get("cluster", {})
    cl = _stage(job, "stage 5 cluster", lambda: cluster_run(
        store, job_id,
        min_cluster_size=_cl_cfg.get("min_cluster_size", 2),
        min_cohesion=_cl_cfg.get("min_cohesion", 0.55),
        cluster_selection_method=_cl_cfg.get("cluster_selection_method", "leaf"),
        progress=lambda d, t: _set_stage_progress(5, d, t, "steps")))
    job.update(clusters=cl["clusters"])

    # Stage 6: demand scoring.
    job.update(status="scoring", stage=6)
    store.set_stage(job_id, 6, "scoring")
    _set_stage_progress(6, 0, 0, "")
    from pipeline.s6_demand import demand_run
    _stage(job, "stage 6 demand",
           lambda: demand_run(store, job_id, run_cfg.get("scoring_weights", {}),
                              progress=lambda d, t: _set_stage_progress(6, d, t, "themes")))

    # Stage 7: hard filters.
    job.update(status="filtering", stage=7)
    store.set_stage(job_id, 7, "filtering")
    _set_stage_progress(7, 0, 0, "")
    from pipeline.s7_filters import filters_run
    _stage(job, "stage 7 filters",
           lambda: filters_run(store, job_id, run_cfg.get("hard_filters", []),
                               progress=lambda d, t: _set_stage_progress(7, d, t, "themes")))

    # Stage 7.5: soft software-solvability filter (advisory color tag, never drops).
    if run_cfg.get("soft_filter", {}).get("enabled", True):
        _check_cancelled(job)
        job.update(status="soft-filtering")
        from pipeline.s7b_softfilter import softfilter_run
        sf = _stage(job, "stage 7.5 soft-filter",
                    lambda: softfilter_run(store, job_id, run_cfg.get("extract", {}),
                                           progress=lambda d, t: _set_stage_progress(7, d, t, "themes")),
                    advisory=True)
        if sf is not None:
            job.update(solvable=sf.get("counts"))
        else:
            job.update(soft_filter_error="gave up after retries")

    # Stage 8: competition.
    job.update(status="competing", stage=8)
    store.set_stage(job_id, 8, "competing")
    _set_stage_progress(8, 0, 0, "")
    from pipeline.s8_compete import compete_run
    _stage(job, "stage 8 compete",
           lambda: compete_run(store, job_id, run_cfg.get("competitor_sources", []),
                               progress=lambda d, t: _set_stage_progress(8, d, t, "themes")))

    # Stage 9: rank (demand x persistence / saturation x solvable_weight).
    job.update(status="ranking", stage=9)
    store.set_stage(job_id, 9, "ranking")
    _set_stage_progress(9, 0, 0, "")
    from pipeline.s9_rank import rank_run
    _rank_w = run_cfg.get("rank", {}).get("solvable_weights")
    ranked = _stage(job, "stage 9 rank",
                    lambda: rank_run(store, job_id, solvable_weights=_rank_w,
                                     progress=lambda d, t: _set_stage_progress(9, d, t, "themes")))
    job.update(ranked=ranked["ranked"], dropped=ranked["dropped"])

    # Stage 9.5: competitor discovery (advisory). Covers the top `cover_top` themes in
    # one call, backfills real saturation, then we RE-RANK so competition + solvability
    # actually move the order before ideas/reviews use the top-N.
    if run_cfg.get("competitors", {}).get("enabled", True):
        _check_cancelled(job)
        job.update(status="competitor-discovery")
        from pipeline.s9b_competitors import competitors_run
        cmp = _stage(job, "stage 9.5 competitors", lambda: competitors_run(
            store, job_id,
            top_n=run_cfg.get("ideas", {}).get("top_n", 5),
            cover_top=run_cfg.get("competitors", {}).get("cover_top", 20),
            extract_cfg=run_cfg.get("extract", {}),
            progress=lambda d, t: _set_stage_progress(9, d, t, "themes")), advisory=True)
        if cmp is not None:
            job.update(competitors=cmp.get("competitors"))
            # Re-rank now that saturation reflects real competitor counts.
            ranked = _stage(job, "stage 9.7 re-rank",
                            lambda: rank_run(store, job_id, solvable_weights=_rank_w,
                                             progress=lambda d, t: _set_stage_progress(9, d, t, "themes")))
            job.update(ranked=ranked["ranked"], dropped=ranked["dropped"])
        else:
            job.update(competitor_error="gave up after retries")

    # Stage 9.6: mine 1-2 star app-store reviews of those competitors (incumbent gaps).
    rev_cfg = run_cfg.get("reviews", {})
    if rev_cfg.get("enabled", True):
        _check_cancelled(job)
        job.update(status="review-mining")
        from pipeline.reviews import reviews_run
        rv = _stage(job, "stage 9.6 reviews", lambda: reviews_run(
            store, job_id,
            countries=rev_cfg.get("countries", ["us"]),
            max_pages=rev_cfg.get("max_pages", 3),
            max_stars=rev_cfg.get("max_stars", 2),
            max_per_competitor=rev_cfg.get("max_per_competitor", 25),
            progress=lambda d, t: _set_stage_progress(9, d, t, "apps")), advisory=True)
        if rv is not None:
            job.update(reviews=rv.get("reviews"))
        else:
            job.update(review_error="gave up after retries")

    # Stage 10: ideas.
    job.update(status="ideating", stage=10)
    store.set_stage(job_id, 10, "ideating")
    _set_stage_progress(10, 0, 0, "")
    from pipeline.s10_ideas import ideas_run
    ideas = _stage(job, "stage 10 ideas",
                   lambda: ideas_run(store, job_id, run_cfg.get("ideas", {}).get("top_n", 5),
                                     progress=lambda d, t: _set_stage_progress(10, d, t, "ideas")))
    job.update(ideas=ideas["ideas"])

    # Stage 11: validation.
    job.update(status="validating", stage=11)
    store.set_stage(job_id, 11, "validating")
    _set_stage_progress(11, 0, 0, "")
    from pipeline.s11_validation import validation_run
    _stage(job, "stage 11 validation",
           lambda: validation_run(store, job_id,
                                  progress=lambda d, t: _set_stage_progress(11, d, t, "ideas")))

    # Stage 12: report.
    job.update(status="reporting", stage=12)
    store.set_stage(job_id, 12, "reporting")
    _set_stage_progress(12, 0, 0, "")
    from pipeline.s12_report import report_run
    report = _stage(job, "stage 12 report", lambda: report_run(
        store, job_id, str(ROOT / run_cfg.get("report_dir", "reports")),
        progress=lambda d, t: _set_stage_progress(12, d, t, "sections")))
    job.update(report=report["path"])

    store.set_stage(job_id, 12, "done")
    job.update(status="done", stage=12)


def _worker(job_id: str, seed: str, limit: int, use_render: bool, use_firecrawl: bool,
            extractor: str = None, use_corpus: bool = False):
    job = JOBS[job_id]
    store = None
    try:
        store = Store(DB)
        def _set_stage_progress(stage: int, done: int, total: int, unit: str = ""):
            store.set_progress(job_id, stage, done, total, unit)
        run_cfg = _run_config(seed, use_render, use_firecrawl)
        if not job.get("cooldown", True):
            # UI toggled the Reddit backfill rest breaks off: zero the cooldown knobs for
            # this run only (faster, higher ban risk). Replace the reddit sub-dict so CFG
            # stays untouched.
            _coll = run_cfg["collection"]
            _rc = dict(_coll.get("reddit", {}))
            for _k in ("cooldown_every_minutes", "cooldown_minutes",
                       "backfill_cooldown_every_minutes", "backfill_cooldown_minutes"):
                _rc[_k] = 0
            _coll["reddit"] = _rc
        corpus_key = corpus_key_for_seed(seed) if use_corpus else None
        corpus_mode = ""
        if corpus_key:
            store.ensure_corpus(corpus_key, seed)
            corpus_info = store.get_corpus(corpus_key) or {}
            if job.get("historical"):
                corpus_mode = "historical"
            else:
                corpus_mode = "refresh" if corpus_info.get("backfill_completed_at") else "backfill"
            job.update(corpus_key=corpus_key)
            job.update(corpus_mode=corpus_mode)
            # Auto-track any new corpus seed as a Source so it shows up on the Sources /
            # corpora pages and can be re-collected or analyzed later (platform-neutral).
            store.add_source(uuid.uuid4().hex[:12], seed, None, corpus_key)
        providers = _extractor_providers(extractor)
        if providers:  # per-run override of the stage-3 extractor try-list
            run_cfg["extract"] = dict(run_cfg.get("extract", {}))
            run_cfg["extract"]["providers"] = providers

        # Stage 1: collect. Retried with a fresh collector each attempt (browser
        # relaunch), so a Chromium/ICU launch crash self-heals instead of freezing.
        job.update(status="collecting", stage=1)
        store.start_run(job_id, seed, use_render=use_render, use_firecrawl=use_firecrawl,
                        use_corpus=use_corpus, extractor=extractor,
                        historical=bool(job.get("historical")),
                        search_assist=bool(job.get("search_assist")),
                        **_extract_provenance(run_cfg, extractor))
        _set_stage_progress(1, 0, 0, "")

        def _s1_collect():
            clean_finish = True
            heartbeat = {"ts": time.monotonic(), "phase": "starting"}
            inherited = {"docs": 0, "threads": 0, "authors": 0}
            stall_after = CFG.get("collection", {}).get("stall_timeout_seconds", 120)
            watchdog_stop = threading.Event()

            def _beat(phase: str, meta: dict):
                heartbeat["ts"] = time.monotonic()
                heartbeat["phase"] = phase
                _check_cancelled(job)
                note = phase
                if meta.get("sort") and phase in {"sort-start", "sort-rotate", "listing", "listing-stale-stop"}:
                    note = f"{meta['sort']} / {note}"
                if meta.get("next_sort") and phase == "sort-rotate":
                    note += f" -> {meta['next_sort']}"
                if meta.get("remaining_seconds") is not None:
                    mins = max(1, round(int(meta["remaining_seconds"]) / 60))
                    note += f" / {mins}m left"
                if meta.get("page"):
                    note += f" p{meta['page']}"
                if meta.get("discovered") is not None:
                    note += f" / {meta['discovered']} topics"
                if meta.get("comments") is not None:
                    note += f" / {meta['comments']} comments"
                current_url = meta.get("url") or meta.get("permalink")
                if current_url:
                    job.update(current_url=current_url)
                job.update(collection_note=note, collection_phase=phase)
                store.set_run_note(job_id, note)
                display_threads = max(0, store.count_topics(job_id) - inherited["threads"])
                store.set_progress(job_id, 1, display_threads, limit, "topics")

            def _watchdog():
                while not watchdog_stop.wait(5):
                    if time.monotonic() - heartbeat["ts"] > stall_after:
                        job["collection_stalled"] = (
                            f"collection heartbeat stalled during {heartbeat['phase']} for "
                            f">{stall_after}s"
                        )
                        return

            wd = threading.Thread(target=_watchdog, daemon=True)
            wd.start()
            if corpus_key:
                store.link_run_to_corpus(job_id, corpus_key)
                inherited = {
                    "docs": store.count_documents(job_id),
                    "threads": store.count_topics(job_id),
                    "authors": store.count_distinct_authors(job_id),
                }
                store.set_run_inherited_counts(
                    job_id, inherited["docs"], inherited["threads"], inherited["authors"]
                )
                job.update(collector=f"corpus+{corpus_mode or 'sync'}")
            known_thread_urls = store.get_corpus_thread_urls(corpus_key) if corpus_key else None
            extra_thread_urls = None
            if job.get("search_assist") and RedditCollector.is_reddit(seed):
                rc = run_cfg.get("collection", {}).get("reddit", {})
                extra_thread_urls = discover_reddit_thread_urls(
                    seed,
                    limit=int(rc.get("search_assist_limit", 40) or 40),
                    query_templates=rc.get("search_assist_queries"),
                    exclude_thread_urls=known_thread_urls,
                )
                job.update(search_assist_hits=len(extra_thread_urls or []))
            collector, kind = pick_collector(seed, run_cfg, known_thread_urls=known_thread_urls,
                                             corpus_mode=corpus_mode, progress_cb=_beat,
                                             extra_thread_urls=extra_thread_urls)
            job.update(collector=kind if not corpus_key else f"{kind}+corpus-{corpus_mode or 'sync'}")
            persisted_threads = store.count_topics(job_id)
            persisted_docs = store.count_documents(job_id)
            persisted_authors = store.count_distinct_authors(job_id)
            # link_run_to_corpus (above) backfills this job's run_documents with the
            # whole corpus history, so persisted_threads/persisted_docs are the corpus
            # total, not "already saved this run". Real total lives on the subreddit
            # page (/api/corpora); the job UI counter should start at 0 and count only
            # what this run newly collects.
            ui_threads_base = 0 if corpus_key else persisted_threads
            ui_docs_base = 0 if corpus_key else persisted_docs
            ui_authors_base = 0 if corpus_key else persisted_authors
            job.update(new=ui_docs_base, threads=ui_threads_base, authors=ui_authors_base)
            threads, n = set(), persisted_docs
            try:
                for doc in collector.collect(seed, limit):
                    # A yielded doc is itself proof of life: refresh the stall watchdog even
                    # for collectors that don't emit progress_cb heartbeats (XenForo,
                    # Discourse, Firecrawl, Playwright). Without this, the 120s watchdog
                    # falsely kills any such collection that simply runs longer than 120s.
                    heartbeat["ts"] = time.monotonic()
                    # Live 403/429 counter: collectors that adaptively throttle expose
                    # rate_limit_hits (XenForo today); surface it so the UI can show it.
                    rl_hits = getattr(collector, "rate_limit_hits", 0)
                    if rl_hits != job.get("rate_limit_hits"):
                        job.update(rate_limit_hits=rl_hits)
                    if job.get("collection_stalled"):
                        raise CollectionStalled(job["collection_stalled"])
                    _check_cancelled(job)
                    topic_key = doc.thread_url or doc.title or doc.source_url
                    topic_already_seen = store.run_has_topic(job_id, topic_key)
                    if store.upsert_document(job_id, doc):
                        n += 1
                        if not topic_already_seen:
                            threads.add(topic_key)
                            store.set_last_topic_found_at(job_id, doc.fetched_at)
                            job.update(last_topic_found_at=doc.fetched_at)
                        if corpus_key:
                            did = store.get_document_id_by_source_url(doc.source_url)
                            if did:
                                store.link_document_to_corpus(corpus_key, did, doc.fetched_at)
                        display_authors = store.count_distinct_authors(job_id)
                        if corpus_key:
                            display_authors = max(0, display_authors - inherited["authors"])
                        job.update(new=ui_docs_base + (n - persisted_docs),
                                   threads=ui_threads_base + len(threads),
                                   authors=display_authors)
                        if n % 5 == 0:  # topics collected / max-topics cap (best-effort bar)
                            store.set_progress(job_id, 1, ui_threads_base + len(threads), limit, "topics")
            except Exception as e:
                if n > persisted_docs and _is_collect_pause_worthy(e):
                    clean_finish = False
                    job.update(collection_warning=str(e), status="collecting-paused")
                    store.set_progress(job_id, 1, ui_threads_base + len(threads),
                                       max(ui_threads_base + len(threads), 1), "topics")
                    return max(0, n - persisted_docs), clean_finish
                raise
            finally:
                watchdog_stop.set()
                job.update(rate_limit_hits=getattr(collector, "rate_limit_hits", 0))
            return max(0, n - persisted_docs), clean_finish

        new, clean_collect = _stage(job, "stage 1 collect", _s1_collect)
        if new == 0:
            # A corpus refresh over an already-populated corpus can legitimately find nothing
            # new (all recent threads already collected). That's a no-op success, not a failure.
            if corpus_key and store.get_corpus_thread_urls(corpus_key):
                job.update(status="done", stage=2,
                           note="no new posts; corpus already current")
                store.set_stage(job_id, 2, "done")
                store.set_run_note(job_id, "no new posts; corpus already current")
                return
            raise RuntimeError(
                f"collection saved 0 posts from {seed}; stopping before report."
            )

        # Stage 2: store.
        _check_cancelled(job)
        job.update(status="storing", stage=2)
        store.set_stage(job_id, 2, "stored")
        _set_stage_progress(2, 1, 1, "steps")
        if use_corpus:
            if corpus_key and corpus_mode == "backfill" and clean_collect:
                store.mark_corpus_backfilled(corpus_key)
            job.update(status="done", stage=2)
            store.set_stage(job_id, 2, "done")
            return

        # Stages 3-12 (extract -> report). Shared with analyze-only corpus runs.
        _analyze_stages(job, job_id, store, run_cfg)
    except RunCancelled as e:
        job.update(status="cancelled", error=str(e), stop_requested=False)
        if store:
            store.cancel_run(job_id, job.get("stage", 1), str(e))
    except Exception as e:  # Surface to UI AND persist, so DB reflects truth after JOBS is gone.
        import traceback
        traceback.print_exc()
        job.update(status="error", error=str(e))
        if store:
            try:
                store.fail_run(job_id, job.get("stage", 1), str(e))
            except Exception:
                traceback.print_exc()
    finally:
        if store:
            store.close()


def _discover_worker(did: str, topic: str, limit: int):
    d = DISCOVERIES[did]
    try:
        # Firecrawl is always the discovery backend; force it in the probe cfg so generic
        # candidates get the cheap structural check. Reddit/Discourse still auto-route first.
        cfg = _run_config(topic, False, True)
        dcfg = cfg.get("discover", {})
        from pipeline.discover import firecrawl_search, validate_candidate

        d.update(status="searching")
        cands = firecrawl_search(topic, limit)
        d.update(candidates=[c.as_dict() for c in cands])  # show results before validating
        if not cands:
            d.update(status="done", error="no candidates found for this topic")
            return

        d.update(status="validating")
        for i, c in enumerate(cands):
            validate_candidate(
                c, cfg,
                min_authors=dcfg.get("min_authors", 3),
                probe_limit=dcfg.get("probe_limit", 5),
                min_threads=dcfg.get("min_threads", 5),
            )
            d["candidates"][i] = c.as_dict()
            d.update(validated=i + 1)
        d.update(status="done")
    except Exception as e:
        import traceback
        traceback.print_exc()
        d.update(status="error", error=str(e))


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX


@app.get("/subreddits")
def subreddits_page():
    # Corpora view merged into the Sources page; keep the old path working.
    return RedirectResponse(url="/sources", status_code=307)


@app.get("/sources", response_class=HTMLResponse)
def sources_page():
    return (BASE / "static" / "sources.html").read_text(encoding="utf-8")


@app.post("/api/discover")
def start_discover(req: DiscoverReq):
    topic = (req.topic or "").strip()
    if not topic:
        return JSONResponse({"error": "topic required"}, status_code=400)
    if not req.use_firecrawl:
        return JSONResponse(
            {"error": "Firecrawl required for topic discovery; enable Firecrawl."},
            status_code=400)
    limit = req.limit or CFG.get("discover", {}).get("candidate_limit", 8)
    did = uuid.uuid4().hex[:12]
    DISCOVERIES[did] = dict(discover_id=did, topic=topic, status="starting",
                            candidates=[], validated=0, error=None)
    threading.Thread(target=_discover_worker,
                     args=(did, topic, limit), daemon=True).start()
    return {"discover_id": did}


@app.get("/api/discover/{discover_id}")
def discover_status(discover_id: str):
    d = DISCOVERIES.get(discover_id)
    if not d:
        return JSONResponse({"error": "unknown discovery"}, status_code=404)
    return d


def _register_job(seed: str, use_render: bool, use_firecrawl: bool, use_corpus: bool,
                   extractor: Optional[str], cooldown: bool = True,
                   historical: bool = False, search_assist: bool = False) -> tuple[str, int]:
    """Build the JOBS entry for a seed and return (job_id, limit). Does not start the
    worker thread -- callers decide whether to launch it in the background (/api/run)
    or run it inline on a queue thread (sequential kickoff)."""
    if use_corpus:
        limit = CFG.get("collection", {}).get("corpus_max_threads", 10000)
    else:
        limit = CFG.get("collection", {}).get("max_threads", 100)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = dict(job_id=job_id, seed=seed, stage=1, status="starting",
                        collector=None, new=0, threads=0, authors=0,
                        pains=0, clusters=0, ranked=0, dropped=0,
                        ideas=0, report=None, error=None, rate_limit_hits=0,
                        stop_requested=False, stop_reason=None,
                        use_render=use_render,
                        use_firecrawl=use_firecrawl,
                        use_corpus=use_corpus,
                        historical=historical,
                        search_assist=search_assist,
                        cooldown=cooldown,
                        extractor=extractor or "claude")
    return job_id, limit


def _analyze_worker(job_id: str, corpus_key: str, seed: str, extractor: Optional[str] = None):
    """Analyze-only run: stages 3-15 over an already-collected corpus, no scraping.
    Pulls every corpus document into this run (link_run_to_corpus) so the extract ->
    report pipeline reads them exactly as a fresh run would."""
    job = JOBS[job_id]
    store = None
    try:
        store = Store(DB)
        run_cfg = _run_config(seed, False, False)
        providers = _extractor_providers(extractor)
        if providers:
            run_cfg["extract"] = dict(run_cfg.get("extract", {}))
            run_cfg["extract"]["providers"] = providers
        job.update(status="extracting", stage=3, corpus_key=corpus_key,
                   collector=f"analyze:{corpus_key}")
        store.start_run(job_id, seed, use_corpus=True, extractor=extractor,
                        **_extract_provenance(run_cfg, extractor))
        store.link_run_to_corpus(job_id, corpus_key)
        if store.count_documents(job_id) == 0:
            raise RuntimeError(f"corpus {corpus_key} has no collected documents to analyze.")
        _analyze_stages(job, job_id, store, run_cfg)
    except RunCancelled as e:
        job.update(status="cancelled", error=str(e), stop_requested=False)
        if store:
            store.cancel_run(job_id, job.get("stage", 3), str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        job.update(status="error", error=str(e))
        if store:
            try:
                store.fail_run(job_id, job.get("stage", 3), str(e))
            except Exception:
                traceback.print_exc()
    finally:
        if store:
            store.close()


def _merge_analyze_worker(job_id: str, sources: list[dict], extractor: Optional[str] = None,
                          label: Optional[str] = None):
    """Analyze selected corpora as one merged run, producing one combined report."""
    job = JOBS[job_id]
    store = None
    seed = label or "merged:" + ",".join(s.get("corpus_key") or s["url"] for s in sources)
    try:
        store = Store(DB)
        run_cfg = _run_config(sources[0]["url"], False, False)
        providers = _extractor_providers(extractor)
        if providers:
            run_cfg["extract"] = dict(run_cfg.get("extract", {}))
            run_cfg["extract"]["providers"] = providers
        job.update(status="extracting", stage=3, collector="analyze:merged",
                   corpus_key=",".join(s.get("corpus_key") or s["url"] for s in sources))
        store.start_run(job_id, seed, use_corpus=True, extractor=extractor,
                        **_extract_provenance(run_cfg, extractor))
        linked = 0
        for src in sources:
            corpus_key = src.get("corpus_key")
            if not corpus_key:
                continue
            linked += store.link_run_to_corpus(job_id, corpus_key)
        if store.count_documents(job_id) == 0:
            raise RuntimeError("selected sources have no collected documents to analyze.")
        job.update(new=store.count_documents(job_id),
                   threads=store.count_topics(job_id),
                   authors=store.count_distinct_authors(job_id),
                   merged_sources=len(sources), linked_documents=linked)
        _analyze_stages(job, job_id, store, run_cfg)
    except RunCancelled as e:
        job.update(status="cancelled", error=str(e), stop_requested=False)
        if store:
            store.cancel_run(job_id, job.get("stage", 3), str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        job.update(status="error", error=str(e))
        if store:
            try:
                store.fail_run(job_id, job.get("stage", 3), str(e))
            except Exception:
                traceback.print_exc()
    finally:
        if store:
            store.close()


@app.post("/api/run")
def start_run(req: RunReq):
    seed = normalize_seed(req.seed_url)
    if not seed:
        return JSONResponse({"error": "seed_url required"}, status_code=400)
    if req.collector:
        use_render, use_firecrawl = _collector_to_toggles(req.collector)
    else:
        use_render, use_firecrawl = req.use_render, req.use_firecrawl
    job_id, limit = _register_job(seed, use_render, use_firecrawl,
                                  req.use_corpus, req.extractor, req.cooldown,
                                  req.historical, req.search_assist)
    if req.limit and not req.use_corpus:
        limit = req.limit
    threading.Thread(target=_worker, args=(job_id, seed, limit, use_render,
                     use_firecrawl, req.extractor, req.use_corpus), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/sources")
def add_source(req: SourceReq):
    seed = normalize_seed(req.url)
    if not seed:
        return JSONResponse({"error": "url required"}, status_code=400)
    store = Store(DB)
    try:
        source_id = uuid.uuid4().hex[:12]
        corpus_key = corpus_key_for_seed(seed)
        added = store.add_source(source_id, seed, (req.label or "").strip() or None, corpus_key)
        if not added:
            return JSONResponse({"error": "source already tracked"}, status_code=409)
        return {"id": source_id}
    finally:
        store.close()


@app.get("/api/sources")
def list_sources():
    store = Store(DB)
    try:
        return store.list_sources()
    finally:
        store.close()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str):
    store = Store(DB)
    try:
        if not store.delete_source(source_id):
            return JSONResponse({"error": "unknown source"}, status_code=404)
        return {"ok": True}
    finally:
        store.close()


def _kickoff_worker(kickoff_id: str, sources: list[dict], mode: str, use_render: bool,
                    use_firecrawl: bool, use_corpus: bool, extractor: Optional[str],
                    cooldown: bool = True, historical: bool = False,
                    search_assist: bool = False):
    kickoff = KICKOFFS[kickoff_id]
    store = Store(DB)
    try:
        for i, src in enumerate(sources):
            if kickoff.get("stop_requested"):
                kickoff["status"] = "stopped"
                return
            seed = src["url"]
            items = kickoff.setdefault("items", [])
            if i < len(items):
                item = items[i]
            else:
                item = dict(source_id=src["id"], seed=seed, label=src.get("label"),
                            corpus_key=src.get("corpus_key"), status="queued",
                            job_id=None, stage=None, error=None)
                items.append(item)
            kickoff["current_index"] = i
            kickoff["current_source"] = src.get("label") or seed
            store.mark_source_queued(src["id"])
            try:
                if mode == "analyze":
                    corpus_key = src.get("corpus_key") or corpus_key_for_seed(seed)
                    job_id, _ = _register_job(seed, False, False, True, extractor, cooldown,
                                              historical, search_assist)
                    kickoff["current_job_id"] = job_id
                    kickoff["job_ids"].append(job_id)
                    item.update(status="running", job_id=job_id)
                    _analyze_worker(job_id, corpus_key, seed, extractor)
                else:
                    job_id, limit = _register_job(seed, use_render, use_firecrawl, use_corpus,
                                                  extractor, cooldown, historical, search_assist)
                    kickoff["current_job_id"] = job_id
                    kickoff["job_ids"].append(job_id)
                    item.update(status="running", job_id=job_id)
                    _worker(job_id, seed, limit, use_render, use_firecrawl, extractor, use_corpus)
                final_job = JOBS.get(job_id, {})
                item.update(status=final_job.get("status", "done"),
                            stage=final_job.get("stage"),
                            error=final_job.get("error"))
            except Exception as e:
                item.update(status="error", error=str(e))
                print(f"[kickoff] {kickoff_id} ({mode}) source {seed} failed: {e}", flush=True)
            kickoff["done"] = i + 1
        kickoff["status"] = "done"
    finally:
        store.close()


@app.post("/api/sources/kickoff")
def kickoff_sources(req: KickoffReq):
    mode = req.mode if req.mode in ("collect", "analyze") else "collect"
    ids = list(dict.fromkeys(req.source_ids))  # de-dupe, keep order
    # Collect kickoff is deliberately batch-only (3-15). Analyze can run on a single
    # already-collected corpus, so it only needs the upper bound.
    lo = 1 if mode == "analyze" else KICKOFF_MIN
    if not (lo <= len(ids) <= KICKOFF_MAX):
        return JSONResponse(
            {"error": f"pick between {lo} and {KICKOFF_MAX} sources (got {len(ids)})"},
            status_code=400)
    store = Store(DB)
    try:
        sources = []
        for sid in ids:
            src = store.get_source(sid)
            if not src:
                return JSONResponse({"error": f"unknown source {sid}"}, status_code=404)
            sources.append(src)
    finally:
        store.close()
    if req.collector:
        use_render, use_firecrawl = _collector_to_toggles(req.collector)
    else:
        use_render, use_firecrawl = req.use_render, req.use_firecrawl
    kickoff_id = uuid.uuid4().hex[:12]
    KICKOFFS[kickoff_id] = dict(kickoff_id=kickoff_id, mode=mode, total=len(sources), done=0,
                                current_index=None, current_source=None, current_job_id=None,
                                job_ids=[], status="running", stop_requested=False,
                                items=[dict(source_id=s["id"], seed=s["url"],
                                            label=s.get("label"),
                                            corpus_key=s.get("corpus_key"),
                                            status="queued", job_id=None,
                                            stage=None, error=None)
                                       for s in sources])
    threading.Thread(target=_kickoff_worker,
                     args=(kickoff_id, sources, mode, use_render, use_firecrawl,
                           req.use_corpus, req.extractor, req.cooldown,
                           req.historical, req.search_assist), daemon=True).start()
    return {"kickoff_id": kickoff_id}


@app.post("/api/sources/merge-analyze")
def merge_analyze_sources(req: MergeAnalyzeReq):
    ids = list(dict.fromkeys(req.source_ids))
    if not (2 <= len(ids) <= KICKOFF_MAX):
        return JSONResponse(
            {"error": f"pick between 2 and {KICKOFF_MAX} sources to merge (got {len(ids)})"},
            status_code=400)
    store = Store(DB)
    try:
        sources = []
        missing = []
        empty = []
        for sid in ids:
            src = store.get_source(sid)
            if not src:
                missing.append(sid)
                continue
            if not src.get("corpus_key"):
                return JSONResponse({"error": f"source {sid} has no corpus key"}, status_code=400)
            if store.count_corpus_documents(src["corpus_key"]) == 0:
                empty.append(src["corpus_key"])
            sources.append(src)
        if missing:
            return JSONResponse({"error": f"unknown source(s): {', '.join(missing)}"}, status_code=404)
        if empty:
            return JSONResponse({"error": f"empty corpus/corpora: {', '.join(empty)}"}, status_code=400)
    finally:
        store.close()

    label = (req.label or "").strip()
    if not label:
        label = "merged:" + "+".join(s.get("corpus_key") or s["url"] for s in sources)
    job_id, _ = _register_job(label, False, False, True, req.extractor)
    JOBS[job_id].update(stage=3, status="queued", collector="analyze:merged",
                        merged_sources=len(sources))
    threading.Thread(target=_merge_analyze_worker,
                     args=(job_id, sources, req.extractor, label),
                     daemon=True).start()
    return {"job_id": job_id, "merged_sources": len(sources)}


def _run_status_summary(job_id: str, store) -> dict | None:
    job = JOBS.get(job_id)
    run = store.get_run(job_id)
    if not job and not run:
        return None
    out = dict(job or {})
    if run:
        out.setdefault("job_id", run["job_id"])
        out.setdefault("seed", run["seed_url"])
        out.setdefault("stage", run["stage"])
        out.setdefault("status", run["status"])
        out.setdefault("error", run["error"])
        out.setdefault("extractor", run.get("extractor"))
        out.setdefault("extract_provider", run.get("extract_provider"))
        out.setdefault("extract_model", run.get("extract_model"))
        out.setdefault("extract_base_url", run.get("extract_base_url"))
        out.setdefault("prompt_version", run.get("prompt_version"))
        out.setdefault("use_corpus", run.get("use_corpus"))
        out["created_at"] = run.get("created_at")
        out["updated_at"] = run.get("updated_at")
    try:
        counts = _persisted_run_counts(job_id, store)
        out.update(counts)
    except Exception:
        pass
    progress = _progress_for(job_id, store)
    out["progress"] = progress
    out["pct"] = progress.get("pct") if progress else None
    return {
        "job_id": out.get("job_id", job_id),
        "seed": out.get("seed"),
        "stage": out.get("stage"),
        "status": out.get("status"),
        "error": out.get("error"),
        "progress": out.get("progress"),
        "pct": out.get("pct"),
        "extractor": out.get("extractor"),
        "extract_provider": out.get("extract_provider"),
        "extract_model": out.get("extract_model"),
        "extract_base_url": out.get("extract_base_url"),
        "prompt_version": out.get("prompt_version"),
        "use_corpus": out.get("use_corpus"),
        "new": out.get("new"),
        "threads": out.get("threads"),
        "authors": out.get("authors"),
        "pains": out.get("pains"),
        "clusters": out.get("clusters"),
        "ranked": out.get("ranked"),
        "ideas": out.get("ideas"),
        "created_at": out.get("created_at"),
        "updated_at": out.get("updated_at"),
    }


@app.get("/api/sources/kickoff/{kickoff_id}")
def kickoff_status(kickoff_id: str):
    k = KICKOFFS.get(kickoff_id)
    if not k:
        return JSONResponse({"error": "unknown kickoff"}, status_code=404)
    out = dict(k)
    store = Store(DB)
    try:
        enriched = []
        for item in out.get("items", []):
            row = dict(item)
            job_id = row.get("job_id")
            if job_id:
                row["run"] = _run_status_summary(job_id, store)
            enriched.append(row)
        out["items"] = enriched
        if not enriched:
            out["items"] = [
                {"job_id": job_id, "status": "running",
                 "run": _run_status_summary(job_id, store)}
                for job_id in out.get("job_ids", [])
            ]
        return out
    finally:
        store.close()


@app.post("/api/sources/kickoff/{kickoff_id}/stop")
def kickoff_stop(kickoff_id: str):
    k = KICKOFFS.get(kickoff_id)
    if not k:
        return JSONResponse({"error": "unknown kickoff"}, status_code=404)
    k["stop_requested"] = True
    cur_job = k.get("current_job_id")
    if cur_job and cur_job in JOBS:
        JOBS[cur_job]["stop_requested"] = True
        JOBS[cur_job]["stop_reason"] = "Stopped by user (kickoff cancelled)"
    return {"ok": True}


@app.post("/api/run/{job_id}/stop")
def stop_run(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        store = Store(DB)
        try:
            run = store.get_run(job_id)
            if not run:
                return JSONResponse({"error": "unknown job"}, status_code=404)
            if run["status"] in {"done", "error", "cancelled"}:
                return {"job_id": job_id, "status": run["status"], "message": "run already finished"}
            if _resume_runner_alive(job_id):
                _request_resume_stop(job_id)
                store.set_run_status(job_id, run["stage"], "stopping")
                store.set_run_note(job_id, "stop requested from UI")
                return {"job_id": job_id, "status": "stopping"}
        finally:
            store.close()
        return JSONResponse({"error": "run is not active in this server process"}, status_code=409)
    if job["status"] in {"done", "error", "cancelled"}:
        return {"job_id": job_id, "status": job["status"], "message": "run already finished"}
    job.update(stop_requested=True, stop_reason="Stopped by user", status="stopping")
    return {"job_id": job_id, "status": "stopping"}


def _resume_log_progress(job_id: str):
    """Fallback extract progress for a run driven by an out-of-process resume runner
    that predates run_progress: parse the last `docs=<done>/<total>` from its logfile."""
    p = ROOT / "db" / f"resume-{job_id}.log"
    if not p.exists():
        return None
    try:
        matches = re.findall(r"docs=(\d+)/(\d+)", p.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return None
    if not matches:
        return None
    done, total = int(matches[-1][0]), int(matches[-1][1])
    return {"stage": 3, "done": done, "total": total, "unit": "docs",
            "pct": round(100 * done / total) if total else None}


def _progress_for(job_id: str, store):
    return store.get_progress(job_id) or _resume_log_progress(job_id)


def _resume_stop_path(job_id: str) -> Path:
    return ROOT / "db" / f"resume-{job_id}.stop"


def _request_resume_stop(job_id: str, reason: str = "Stopped by user") -> None:
    p = _resume_stop_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reason, encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID is currently running. Cross-platform, no psutil."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _resume_runner_alive(job_id: str) -> bool:
    """A resume runner already owns this job if its pidfile names a live process. A stale
    pidfile (missing or dead PID) means the job is free to (re)spawn."""
    p = ROOT / "db" / f"resume-{job_id}.pid"
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _spawn_resume_runner(job_id: str):
    kwargs = dict(
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [sys.executable, "-m", "pipeline.resume", job_id],
        **kwargs,
    )


def _reconcile_orphaned_runs():
    store = Store(DB)
    try:
        orphaned = store.get_runs_by_status(sorted(ACTIVE_RUN_STATUSES))
        for run in orphaned:
            job_id = run["job_id"]
            # A live resume runner already owns this job (e.g. this is a repeat restart
            # while a prior resume is still going) -- don't stack a second runner on it.
            if _resume_runner_alive(job_id):
                continue
            try:
                store.set_run_status(
                    job_id, run["stage"], "recovering",
                    "Server restarted while this run was active; auto-resume launched.")
                _spawn_resume_runner(job_id)
            except Exception:
                store.set_run_status(
                    job_id, run["stage"], "interrupted",
                    "Server restarted while this run was active; auto-resume failed.")
    finally:
        store.close()


def _persisted_run_counts(job_id: str, store):
    """Read the persisted counters for a run from SQLite.

    The live in-memory job dict can temporarily lag behind the database during
    retries, especially in stage 1 where a fresh collector attempt restarts the
    local `new` counter. Using persisted totals keeps status responses stable.
    """
    return {
        **store.get_run_display_counts(job_id),
        "pains": store.count_pains(job_id),
        "clusters": store.count_clusters(job_id),
        "ranked": len(store.get_ranked_clusters(job_id, include_dropped=True)),
        "ideas": len(store.get_ideas(job_id)),
    }


@app.get("/api/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if job:
        out = dict(job)
        store = Store(DB)
        try:
            run_row = store.get_run(job_id) or {}
            persisted = _persisted_run_counts(job_id, store)
            for key, value in persisted.items():
                out[key] = max(out.get(key, 0) or 0, value)
            out["progress"] = _progress_for(job_id, store)
            out["note"] = out.get("collection_note") or run_row.get("note")
            out["last_topic_found_at"] = out.get("last_topic_found_at") or store.get_last_topic_found_at(job_id, run_row)
            out["created_at"] = run_row.get("created_at")
            out["updated_at"] = run_row.get("updated_at")
        finally:
            store.close()
        return out
    # Not in this process's memory (e.g. server restarted) — fall back to persisted state.
    store = Store(DB)
    run = store.get_run(job_id)
    store.close()
    if not run:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    store = Store(DB)
    counts = _persisted_run_counts(job_id, store)
    report_row = store.get_report(job_id)
    progress = _progress_for(job_id, store)
    last_topic_found_at = store.get_last_topic_found_at(job_id, run)
    store.close()
    return {"job_id": run["job_id"], "seed": run["seed_url"], "stage": run["stage"],
            "status": run["status"], "error": run["error"], "collector": None,
            "note": run.get("note"),
            "extractor": run.get("extractor"),
            "extract_provider": run.get("extract_provider"),
            "extract_model": run.get("extract_model"),
            "extract_base_url": run.get("extract_base_url"),
            "prompt_version": run.get("prompt_version"),
            "last_topic_found_at": last_topic_found_at,
            "created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
            "new": counts["new"], "threads": counts["threads"], "authors": counts["authors"],
            "pains": counts["pains"], "clusters": counts["clusters"], "ranked": counts["ranked"],
            "dropped": None, "ideas": counts["ideas"],
            "stop_requested": run["status"] == "cancelled", "progress": progress,
            "report": report_row["path"] if report_row else None, "persisted_only": True}


@app.on_event("startup")
def _startup_reconcile_runs():
    _reconcile_orphaned_runs()
    # Backfill Source rows for corpora collected before the sources table existed, so the
    # merged Sources page lists everything (not just newly-added seeds).
    store = Store(DB)
    try:
        n = store.backfill_sources_from_corpora()
        if n:
            print(f"[startup] backfilled {n} source(s) from existing corpora", flush=True)
    finally:
        store.close()


@app.get("/api/runs")
def runs(limit: int = 25, offset: int = 0, q: str = "", status: str = ""):
    store = Store(DB)
    items = store.list_runs(limit=limit, offset=offset, q=q, status=status)
    total = store.count_runs(q=q, status=status)
    store.close()
    return {"items": items, "total": total}


@app.get("/api/active-runs")
def active_runs():
    store = Store(DB)
    try:
        active_ids = {row["job_id"] for row in store.get_runs_by_status(sorted(ACTIVE_RUN_STATUSES))}
        active_ids.update(
            job_id for job_id, job in JOBS.items()
            if job.get("status") in ACTIVE_RUN_STATUSES
        )
        items = [
            summary for job_id in active_ids
            if (summary := _run_status_summary(job_id, store))
        ]
        items.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
        return {"items": items}
    finally:
        store.close()


@app.get("/api/corpora")
def corpora(prefix: str = "reddit:"):
    store = Store(DB)
    out = store.list_corpora(prefix=prefix or "")
    store.close()
    return out


@app.delete("/api/run/{job_id}")
def delete_run(job_id: str):
    job = JOBS.get(job_id)
    if job and job.get("status") in ACTIVE_RUN_STATUSES:
        return JSONResponse(
            {"error": "run is active; stop it before deleting"}, status_code=409)
    store = Store(DB)
    if not store.get_run(job_id):
        store.close()
        return JSONResponse({"error": "unknown job"}, status_code=404)
    info = store.delete_run(job_id)
    store.close()
    JOBS.pop(job_id, None)
    rp = info.get("report_path")
    if rp:
        try:
            Path(rp).unlink(missing_ok=True)
        except OSError:
            pass
    return {"job_id": job_id, "deleted": True}


@app.get("/api/run/{job_id}/docs")
def docs(job_id: str, limit: int = 60):
    store = Store(DB)
    out = store.get_document_rows(job_id, limit)
    store.close()
    return out


@app.get("/api/run/{job_id}/pains")
def pains(job_id: str, limit: int = 0):
    """All pains for a run (limit<=0 = all), each tagged with its theme so the UI can
    filter by persona/theme and link every comment to its source permalink."""
    store = Store(DB)
    sql = ("SELECT p.persona, p.complaint, p.workflow_pain, p.wish, p.verbatim_span, "
           "COALESCE(p.source_permalink, p.source_id), p.span_start, p.span_end, "
           "c.label, c.id, p.workaround, sf.solvable, p.persona_canonical "
           "FROM pains p "
           "LEFT JOIN cluster_members cm ON cm.pain_id=p.id "
           "LEFT JOIN clusters c ON c.id=cm.cluster_id AND c.run_id=p.run_id "
           "LEFT JOIN soft_filters sf ON sf.cluster_id=c.id AND sf.run_id=p.run_id "
           "WHERE p.run_id=?")
    params = [job_id]
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    rows = store.conn.execute(sql, params).fetchall()
    store.close()
    return [{"persona": r[0], "complaint": r[1], "workflow_pain": r[2],
             "wish": r[3], "span": r[4], "source": r[5],
             "span_start": r[6], "span_end": r[7],
             "theme": r[8], "cluster_id": r[9],
             "workaround": r[10], "solvable": r[11],
             "persona_canonical": r[12] or r[0]} for r in rows]


@app.get("/api/run/{job_id}/clusters")
def clusters(job_id: str):
    store = Store(DB)
    out = store.get_clusters(job_id)
    store.close()
    return out


@app.get("/api/run/{job_id}/cluster-details")
def cluster_details(job_id: str):
    """Each theme with its member pains (persona, text, source permalink) — powers the
    per-theme evidence/source list under the ranked themes."""
    store = Store(DB)
    out = store.get_cluster_details(job_id)
    store.close()
    return out


@app.get("/api/run/{job_id}/rankings")
def rankings(job_id: str):
    store = Store(DB)
    out = store.get_ranked_clusters(job_id, include_dropped=True)
    store.close()
    return out


@app.get("/api/run/{job_id}/ideas")
def ideas(job_id: str):
    store = Store(DB)
    out = store.get_ideas(job_id)
    store.close()
    return out


@app.get("/api/run/{job_id}/competitors")
def competitors(job_id: str):
    store = Store(DB)
    out = store.get_competitors(job_id)
    store.close()
    return out


@app.get("/api/run/{job_id}/reviews")
def reviews(job_id: str):
    store = Store(DB)
    out = store.get_reviews(job_id)
    store.close()
    return out


@app.get("/api/run/{job_id}/report", response_class=PlainTextResponse)
def report(job_id: str):
    store = Store(DB)
    row = store.get_report(job_id)
    store.close()
    if not row:
        return PlainTextResponse("report not found", status_code=404)
    path = Path(row["path"])
    if not path.exists():
        return PlainTextResponse("report file missing", status_code=404)
    return path.read_text(encoding="utf-8")
