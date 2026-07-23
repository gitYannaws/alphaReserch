"""Crash-resilient resume runner for a stuck / orphaned Sales Safari run.

Why this exists: the web app runs the whole pipeline in a single *daemon*
thread inside uvicorn (webapp/app.py). If the uvicorn process dies mid-run
(e.g. the Playwright/Chromium subprocess crashes -- see the ICU
`Invalid file descriptor to ICU data received` class of failure), the daemon
thread dies with it, no cleanup, no error persisted. The `runs` row is frozen
at whatever it last wrote (`collecting`, stage 1) and nothing ever resumes it.

This runner fixes that for an existing run:
  * runs OUTSIDE uvicorn, in its own process' main thread, so a subprocess
    crash cannot silently kill it,
  * auto-detects how far the run got (from stored artifacts) and starts at the
    first incomplete stage -- reusing already-collected/extracted data,
  * wraps every stage in retry + exponential backoff, and re-launches the
    collector fresh on each collection retry (browser relaunch),
  * persists stage/status to the DB after every step, so status is never frozen,
  * emits live STATUS / OK / RETRY / NOTIFY lines to stdout and a logfile, and
    exits non-zero on an unrecovered crash so a supervisor can notify.

Usage:
    python -m pipeline.resume <job_id>
    python -m pipeline.resume <job_id> --from 3 --attempts 3 --base-delay 5

Stage 3 (extraction) needs the `claude` CLI logged in.
"""
import argparse
import copy
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from pipeline.collectors.reddit_collector import RedditCollector
from pipeline.discover import discover_reddit_thread_urls
from pipeline.orchestrate import corpus_key_for_seed, load_config, pick_collector
from pipeline.retry import run_with_retry
from pipeline.store import Store

ROOT = Path(__file__).resolve().parent.parent
load_dotenv()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _is_collect_pause_worthy(error: Exception) -> bool:
    msg = str(error).lower()
    return ("http 429" in msg or "http 403" in msg or "bot/login wall" in msg
            or "access denied" in msg or "too many requests" in msg)


class RunCancelled(Exception):
    pass


CLAUDE_EXTRACTOR_MODELS = {
    "claude": "claude-sonnet-4-6",  # backward-compatible old UI value
    "claude_sonnet": "claude-sonnet-4-6",
    "claude_haiku": "claude-haiku-4-5-20251001",
}

CODEX_EXTRACTOR_MODELS = {
    "codex": "gpt-5.6-terra",
    "codex_gpt56_sol": "gpt-5.6-sol",
    "codex_gpt56_luna": "gpt-5.6-luna",
    "codex_gpt55": "gpt-5.5",
    "codex_gpt54": "gpt-5.4",
    "codex_gpt54_mini": "gpt-5.4-mini",
    "codex_spark": "gpt-5.3-codex-spark",
}


def _extractor_providers(name):
    if name in ("qwen", "qwen3", "glm", "local"):
        return [name, "claude"]
    if name in CLAUDE_EXTRACTOR_MODELS:
        return ["claude", "codex"]
    if name in CODEX_EXTRACTOR_MODELS:
        return ["codex"]
    return None


def _run_config(base_cfg: dict, run: dict) -> dict:
    cfg = copy.deepcopy(base_cfg)
    coll = dict(cfg.get("collection", {}))
    coll["playwright"] = dict(coll.get("playwright", {}))
    use_render = bool(run.get("use_render"))
    use_firecrawl = bool(run.get("use_firecrawl"))
    if use_firecrawl and use_render:
        coll["fallback"] = "auto"
    elif use_render:
        coll["fallback"] = "render"
    elif use_firecrawl:
        coll["fallback"] = "firecrawl"
    else:
        coll["fallback"] = "none"
    # Reddit backfill rest breaks: run.cooldown==0 means the UI toggled them off for this
    # run (faster, higher ban risk). Mirror webapp _worker: zero the cooldown knobs. NULL/1
    # (default, or any pre-cooldown-column run) keeps config's cooldowns on.
    if run.get("cooldown") == 0:
        reddit = dict(coll.get("reddit", {}))
        for _k in ("cooldown_every_minutes", "cooldown_minutes",
                   "backfill_cooldown_every_minutes", "backfill_cooldown_minutes"):
            reddit[_k] = 0
        coll["reddit"] = reddit
    cfg["collection"] = coll
    providers = _extractor_providers(run.get("extractor"))
    if providers:
        cfg["extract"] = dict(cfg.get("extract", {}))
        cfg["extract"]["providers"] = providers
        if run.get("extractor") in CLAUDE_EXTRACTOR_MODELS:
            cfg["extract"]["claude_model"] = CLAUDE_EXTRACTOR_MODELS[run["extractor"]]
        if run.get("extractor") in CODEX_EXTRACTOR_MODELS:
            cfg["extract"]["codex"] = dict(cfg["extract"].get("codex", {}))
            cfg["extract"]["codex"]["model"] = CODEX_EXTRACTOR_MODELS[run["extractor"]]
    return cfg


# Core artifact per stage -> lets us detect where a run left off. Ordered.
# (stage_int, table, produced_by_stage_label)
_CORE_ARTIFACTS = [
    (1, "run_documents"),
    (3, "pains"),
    (4, "embeddings"),
    (5, "clusters"),
    (6, "demand_scores"),
    (7, "soft_filters"),
    (9, "rankings"),
    (10, "ideas"),
    (11, "idea_briefs"),
    (12, "reports"),
]


class Runner:
    def __init__(self, job_id, cfg, db_path, attempts, base_delay, logfile):
        self.job_id = job_id
        self.attempts = attempts
        self.base_delay = base_delay
        self.store = Store(db_path)
        self.run_info = self.store.get_run(job_id) or {}
        self.cfg = _run_config(cfg, self.run_info)
        self.stopfile = ROOT / "db" / f"resume-{job_id}.stop"
        self._log_fh = open(logfile, "a", encoding="utf-8", buffering=1)
        self.log("STATUS", f"resume runner attached to job {job_id}; log -> {logfile}")

    # ---- logging -----------------------------------------------------------
    def log(self, kind, msg):
        line = f"[{_ts()}] {kind}: {msg}"
        print(line, flush=True)
        try:
            self._log_fh.write(line + "\n")
        except Exception:
            pass

    def _set_progress(self, stage: int, done: int, total: int, unit: str = ""):
        self.store.set_progress(self.job_id, stage, done, total, unit)

    def _check_cancelled(self):
        if not self.stopfile.exists():
            return
        try:
            reason = self.stopfile.read_text(encoding="utf-8").strip()
        except OSError:
            reason = ""
        raise RunCancelled(reason or "Stopped by user")

    # ---- resume-point detection -------------------------------------------
    def _count(self, table) -> int:
        try:
            row = self.store.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (self.job_id,)
            ).fetchone()
            return row[0] if row else 0
        except Exception as e:
            self.log("WARN", f"count {table} failed ({e}); treating as 0")
            return 0

    def detect_start(self):
        """First core stage whose artifact is missing. None => already complete."""
        progress = self.store.get_progress(self.job_id)
        if progress and (progress.get("done") or 0) < (progress.get("total") or 0):
            self.log(
                "STATUS",
                f"incomplete progress found: stage={progress['stage']} "
                f"{progress['done']}/{progress['total']} {progress.get('unit') or ''}".strip(),
            )
            return progress["stage"]
        counts = {t: self._count(t) for _, t in _CORE_ARTIFACTS}
        self.log("STATUS", "stored artifacts: "
                 + ", ".join(f"{t}={counts[t]}" for _, t in _CORE_ARTIFACTS))
        for stage, table in _CORE_ARTIFACTS:
            if counts[table] == 0:
                return stage
        return None

    # ---- stage implementations (mirror webapp/app.py _worker) --------------
    def _collect(self, store):
        run = store.get_run(self.job_id)
        seed = run["seed_url"]
        use_corpus = bool(run.get("use_corpus"))
        # Corpus runs always use the corpus cap; a bounded (non-corpus) run honors the
        # per-run thread_limit the UI sent, falling back to config max_threads.
        if use_corpus:
            limit = self.cfg.get("collection", {}).get("corpus_max_threads", 10000)
        else:
            limit = run.get("thread_limit") or self.cfg.get("collection", {}).get("max_threads", 100)
        corpus_key = corpus_key_for_seed(seed) if use_corpus else None
        corpus_mode = ""
        known_thread_urls = None
        known_thread_stats = None
        inherited = {"docs": 0, "threads": 0, "authors": 0}
        if corpus_key:
            store.ensure_corpus(corpus_key, seed)
            store.link_run_to_corpus(self.job_id, corpus_key)
            inherited = store.ensure_run_inherited_counts(self.job_id)
            corpus = store.get_corpus(corpus_key) or {}
            if bool(run.get("historical")):
                corpus_mode = "historical"
            else:
                corpus_mode = "refresh" if corpus.get("backfill_completed_at") else "backfill"
            known_thread_urls = store.get_corpus_thread_urls(corpus_key)
            known_thread_stats = store.get_corpus_thread_stats(corpus_key)
        def _beat(phase, meta):
            self._check_cancelled()
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
            self.store.set_run_note(self.job_id, note)
            display_threads = max(0, self.store.count_topics(self.job_id) - inherited["threads"])
            self.store.set_progress(self.job_id, 1, display_threads, limit, "topics")
        extra_thread_urls = None
        if bool(run.get("search_assist")) and RedditCollector.is_reddit(seed):
            rc = self.cfg.get("collection", {}).get("reddit", {})
            extra_thread_urls = discover_reddit_thread_urls(
                seed,
                limit=int(rc.get("search_assist_limit", 40) or 40),
                query_templates=rc.get("search_assist_queries"),
                exclude_thread_urls=known_thread_urls,
            )
        collector, kind = pick_collector(seed, self.cfg, known_thread_urls=known_thread_urls,
                                         corpus_mode=corpus_mode, progress_cb=_beat,
                                         extra_thread_urls=extra_thread_urls,
                                         known_thread_stats=known_thread_stats)  # fresh collector each retry
        self.log("STATUS", f"collector={kind} seed={seed} limit={limit}")
        persisted_threads = store.count_topics(self.job_id)
        persisted_docs = store.count_documents(self.job_id)
        threads, new = set(), persisted_docs
        try:
            for doc in collector.collect(seed, limit):
                self._check_cancelled()
                topic_key = doc.thread_url or doc.title or doc.source_url
                topic_already_seen = store.run_has_topic(self.job_id, topic_key)
                if store.upsert_document(self.job_id, doc):
                    new += 1
                    if not topic_already_seen:
                        threads.add(topic_key)
                        store.set_last_topic_found_at(self.job_id, doc.fetched_at)
                    if corpus_key:
                        did = store.get_document_id_by_source_url(doc.source_url)
                        if did:
                            store.link_document_to_corpus(corpus_key, did, doc.fetched_at)
                    if new % 25 == 0:
                        display_docs = max(0, new - persisted_docs)
                        display_threads = len(threads)
                        self.log("STATUS", f"collect new={display_docs} topics={display_threads}")
                        self._set_progress(1, display_threads, limit, "topics")
        except Exception as e:
            if new > persisted_docs and _is_collect_pause_worthy(e):
                self.log("NOTIFY", f"collection paused after partial progress: {e}")
                display_threads = len(threads)
                self._set_progress(1, display_threads, max(display_threads, 1), "topics")
                return f"docs={max(0, new - persisted_docs)} topics={display_threads} paused"
            raise
        if new == persisted_docs:
            raise RuntimeError(f"{kind} collection saved 0 posts from {seed}")
        if corpus_key and corpus_mode == "backfill":
            store.mark_corpus_backfilled(corpus_key)
        return f"docs={max(0, new - persisted_docs)} topics={len(threads)}"

    def _extract(self, store):
        from pipeline.extract import extract_run
        ecfg = self.cfg.get("extract", {})
        marker = {"last": 0}

        def prog(d, t, k):
            store.set_progress(self.job_id, 3, d, t, "docs")
            if k and k >= marker["last"] + 25:
                marker["last"] = k
                self.log("STATUS", f"extract pains={k} docs={d}/{t}")

        extract_run(store, self.job_id,
                    batch_size=ecfg.get("batch_size", 6),
                    progress=prog, extract_cfg=ecfg, should_stop=self._check_cancelled)
        n = store.count_pains(self.job_id)
        if n == 0:
            raise RuntimeError(
                f"extraction produced 0 pains from {store.count_documents(self.job_id)} docs")
        return f"pains={n}"

    def _verify(self, store):
        from pipeline.extract import verify_run

        def prog(d, t, k):
            store.set_progress(self.job_id, 3, d, t, "candidates")

        r = verify_run(store, self.job_id, verify_cfg=self.cfg.get("verify", {}),
                       extract_cfg=self.cfg.get("extract", {}),
                       progress=prog, should_stop=self._check_cancelled)
        return (f"verified kept={r['kept']} rejected={r['rejected']} "
                f"types={r['by_type']} failed_batches={r['failed_batches']}")

    def _embed(self, store):
        from pipeline.embed import embed_run
        embed_run(store, self.job_id, self.cfg.get("embed_model", "BAAI/bge-small-en-v1.5"),
                  progress=lambda d, t: self._set_progress(4, d, t, "pains"))
        return f"embeddings={self._count('embeddings')}"

    def _cluster(self, store):
        from pipeline.cluster import cluster_run
        cc = self.cfg.get("cluster", {})
        r = cluster_run(store, self.job_id,
                        min_cluster_size=cc.get("min_cluster_size", 2),
                        min_cohesion=cc.get("min_cohesion", 0.55),
                        cluster_selection_method=cc.get("cluster_selection_method", "leaf"),
                        semantic_refine=cc.get("semantic_refine", True),
                        semantic_label_only=cc.get("semantic_label_only", False),
                        audit_min_cluster_size=cc.get("audit_min_cluster_size", 5),
                        extract_cfg=self.cfg.get("extract", {}),
                        progress=lambda d, t: self._set_progress(5, d, t, "steps"))
        return f"clusters={r['clusters']}"

    def _demand(self, store):
        from pipeline.s6_demand import demand_run
        demand_run(store, self.job_id, self.cfg.get("scoring_weights", {}),
                   progress=lambda d, t: self._set_progress(6, d, t, "themes"))
        return f"demand_scores={self._count('demand_scores')}"

    def _softfilter(self, store):
        """Stage 7b: one advisory pass = software-fit (LLM) + warning tags (regex).
        The old stage-7 filters pass was folded in here."""
        from pipeline.s7b_softfilter import softfilter_run
        sf = softfilter_run(store, self.job_id, self.cfg.get("extract", {}),
                            progress=lambda d, t: self._set_progress(7, d, t, "themes"),
                            batch_size=self.cfg.get("soft_filter", {}).get("batch_size", 40),
                            max_batch_chars=self.cfg.get("soft_filter", {}).get("max_batch_chars", 8000),
                            enabled_filters=self.cfg.get("hard_filters", []))
        return f"solvable={sf.get('counts')} warned={sf.get('flagged')}"

    def _competitors(self, store):
        from pipeline.s9b_competitors import competitors_run
        cc = self.cfg.get("competitors", {})
        cmp = competitors_run(store, self.job_id,
                              extract_cfg=self.cfg.get("extract", {}),
                              model=cc.get("model", "claude-sonnet-5"),
                              batch_size=cc.get("batch_size", 20),
                              verify_urls=cc.get("verify_urls", True),
                              url_timeout=cc.get("url_timeout", 8),
                              progress=lambda d, t: self._set_progress(10, d, t, "ideas"))
        return (f"competitors={cmp.get('competitors')} ideas={cmp.get('ideas')}"
                f"/{cmp.get('covered')} rejected={cmp.get('rejected')}"
                f" unverified={cmp.get('unverified')}")

    def _rank(self, store):
        from pipeline.s9_rank import rank_run
        rank_cfg = self.cfg.get("rank", {})
        r = rank_run(store, self.job_id,
                     solvable_weights=rank_cfg.get("solvable_weights"),
                     min_support=rank_cfg.get("min_support"),
                     progress=lambda d, t: self._set_progress(9, d, t, "themes"))
        return f"ranked={r['ranked']} dropped={r['dropped']}"

    def _personas(self, store):
        from pipeline.s3b_personas import personas_run
        r = personas_run(store, self.job_id,
                         max_segments=self.cfg.get("personas", {}).get("max_segments", 12),
                         extract_cfg=self.cfg.get("extract", {}))
        return f"personas {r.get('distinct')}->{r.get('segments')} segments"

    def _reviews(self, store):
        from pipeline.reviews import reviews_run
        rc = self.cfg.get("reviews", {})
        rv = reviews_run(store, self.job_id,
                         countries=rc.get("countries", ["us"]),
                         max_pages=rc.get("max_pages", 3),
                         max_stars=rc.get("max_stars", 2),
                         max_per_competitor=rc.get("max_per_competitor", 25),
                         progress=lambda d, t: self._set_progress(10, d, t, "apps"))
        return f"reviews={rv.get('reviews')} matched={rv.get('matched')}/{rv.get('competitors')}"

    def _ideas(self, store):
        from pipeline.s10_ideas import ideas_run
        r = ideas_run(store, self.job_id, self.cfg.get("ideas", {}).get("top_n", 5),
                      extract_cfg=self.cfg.get("extract", {}),
                      model=self.cfg.get("ideas", {}).get("model"),
                      progress=lambda d, t: self._set_progress(10, d, t, "ideas"))
        return (f"ideas={r['ideas']} (llm={r.get('from_llm')} "
                f"template={r.get('from_template')} skipped={r.get('skipped')})")

    def _brief(self, store):
        from pipeline.s10b_brief import brief_run
        bc = self.cfg.get("brief", {})
        r = brief_run(store, self.job_id,
                      extract_cfg=self.cfg.get("extract", {}),
                      model=bc.get("model", "claude-sonnet-5"),
                      progress=lambda d, t: self._set_progress(11, d, t, "briefs"))
        return f"briefs={r['briefs']} with_review_evidence={r.get('with_review_evidence')}"

    def _report(self, store):
        from pipeline.s12_report import report_run
        r = report_run(store, self.job_id, str(ROOT / self.cfg.get("report_dir", "reports")),
                       max_ranked_themes=self.cfg.get("report", {}).get("max_ranked_themes", 50),
                       progress=lambda d, t: self._set_progress(12, d, t, "sections"))
        return f"report={r['path']}"

    # ---- plan --------------------------------------------------------------
    def _plan(self):
        """(stage_int, name, status_label, fn, advisory, enabled)."""
        sf_on = self.cfg.get("soft_filter", {}).get("enabled", True)
        cmp_on = self.cfg.get("competitors", {}).get("enabled", True)
        rv_on = self.cfg.get("reviews", {}).get("enabled", True)
        pers_on = self.cfg.get("personas", {}).get("enabled", True)
        vf_on = self.cfg.get("verify", {}).get("enabled", True)
        br_on = self.cfg.get("brief", {}).get("enabled", True)
        # Idea chain order (changed 2026-07-20): rank -> draft idea -> competitors OF that
        # idea -> their low-star reviews -> brief built on the gap those reviews expose.
        # Competitors used to run before rank against raw themes, which both starved the
        # ideas of competitive context (the top-5 ideas had zero competitors between them)
        # and let saturation penalise the themes we understood best. See s9_rank docstring.
        return [
            (1,  "collect",     "collecting",             self._collect,     False, True),
            (3,  "extract",     "extracting",             self._extract,     False, True),
            (3,  "verify",      "verifying",              self._verify,      True,  vf_on),
            (3,  "personas",    "personas",               self._personas,    True,  pers_on),
            (4,  "embed",       "embedding",              self._embed,       False, True),
            (5,  "cluster",     "clustering",             self._cluster,     False, True),
            (6,  "demand",      "scoring",                self._demand,      False, True),
            (7,  "softfilter",  "soft-filtering",         self._softfilter,  True,  sf_on),
            (9,  "rank",        "ranking",                self._rank,        False, True),
            (10, "ideas",       "ideating",               self._ideas,       False, True),
            (10, "competitors", "competitor-discovery",   self._competitors, True,  cmp_on),
            (10, "reviews",     "review-mining",          self._reviews,     True,  rv_on),
            (11, "brief",       "briefing",               self._brief,       True,  br_on),
            (12, "report",      "reporting",              self._report,      False, True),
        ]

    # ---- retrying stage runner --------------------------------------------
    _SKIPPED = object()

    def run_stage(self, stage, name, status, fn, advisory):
        self.store.set_stage(self.job_id, stage, status)
        self._set_progress(stage, 0, 0, "")
        self.log("STATUS", f"stage {stage} {name} start")
        t0 = time.time()
        try:
            result = run_with_retry(
                lambda: fn(self.store), name=f"stage {stage} {name}",
                attempts=self.attempts, base_delay=self.base_delay,
                log=self.log, advisory=advisory, default=self._SKIPPED,
                should_stop=self._check_cancelled, cancel_exc=RunCancelled)
        except RunCancelled as e:
            reason = str(e) or "Stopped by user"
            self.log("NOTIFY", f"run cancelled during stage {stage} {name}: {reason}")
            self.store.cancel_run(self.job_id, stage, reason)
            return "cancelled"
        except Exception as e:  # non-advisory stage exhausted retries
            self.log("NOTIFY", f"CRASH stage {stage} {name}: {e}")
            self.store.fail_run(self.job_id, stage, f"{name}: {e}")
            return False
        if result is self._SKIPPED:
            self.log("NOTIFY", f"advisory stage {stage} {name} skipped after retries")
        else:
            self.log("OK", f"stage {stage} {name} done in {time.time()-t0:.1f}s :: {result}")
        return True

    # ---- drive -------------------------------------------------------------
    def run(self, start):
        plan = [s for s in self._plan() if s[0] >= start and s[5]]
        if not plan:
            self.log("NOTIFY", f"nothing to do; run {self.job_id} already at/after stage {start}")
            return 0
        self.log("STATUS", "plan: " + " -> ".join(f"{s[0]}:{s[1]}" for s in plan))
        for stage, name, status, fn, advisory, _ in plan:
            ok = self.run_stage(stage, name, status, fn, advisory)
            if ok == "cancelled":
                self.log("NOTIFY", f"CANCELLED job {self.job_id} at stage {stage} {name}")
                return 0
            if not ok:
                self.log("NOTIFY", f"ABORTED at stage {stage} {name}; run marked error in DB")
                return 1
            if stage == 1 and bool(self.run_info.get("use_corpus")):
                self.store.set_stage(self.job_id, 2, "done")
                self._set_progress(2, 1, 1, "steps")
                self.log("NOTIFY", f"DONE corpus job {self.job_id}: docs={self.store.count_documents(self.job_id)} "
                         f"topics={self.store.count_topics(self.job_id)}")
                return 0
        self.store.set_stage(self.job_id, 12, "done")
        self.log("NOTIFY", f"DONE job {self.job_id}: pains={self.store.count_pains(self.job_id)} "
                 f"clusters={self.store.count_clusters(self.job_id)} "
                 f"ideas={len(self.store.get_ideas(self.job_id))}")
        return 0

    def close(self):
        try:
            self.store.close()
        finally:
            self._log_fh.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Resume a stuck Sales Safari run, crash-resilient.")
    ap.add_argument("job_id")
    ap.add_argument("--from", dest="from_stage", type=int, default=None,
                    help="force start stage (default: auto-detect from stored artifacts)")
    ap.add_argument("--attempts", type=int, default=None,
                    help="retries per stage (default: config retry.attempts, else 3)")
    ap.add_argument("--base-delay", type=float, default=None,
                    help="backoff base seconds, doubles each retry (default: config retry.base_delay, else 5)")
    args = ap.parse_args(argv)

    cfg = load_config(str(ROOT / "config.yaml"))
    _rcfg = cfg.get("retry", {})
    attempts = args.attempts if args.attempts is not None else _rcfg.get("attempts", 3)
    base_delay = args.base_delay if args.base_delay is not None else _rcfg.get("base_delay", 5.0)
    db_path = str(ROOT / cfg.get("db_path", "db/safari.sqlite"))
    logfile = str(ROOT / "db" / f"resume-{args.job_id}.log")

    # sanity: run must exist
    probe = Store(db_path)
    run = probe.get_run(args.job_id)
    probe.close()
    if not run:
        print(f"NOTIFY: unknown job {args.job_id}", flush=True)
        return 2

    # Ownership pidfile: lets the web app's startup reconcile see that a live resume
    # runner already owns this job, so repeated server restarts don't stack duplicate
    # runners onto the same run. Removed on exit (finally) so a crashed runner's stale
    # file (dead PID) is treated as free.
    pidfile = Path(ROOT) / "db" / f"resume-{args.job_id}.pid"
    try:
        pidfile.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pidfile = None

    runner = Runner(args.job_id, cfg, db_path, attempts, base_delay, logfile)
    try:
        start = args.from_stage if args.from_stage is not None else runner.detect_start()
        if start is None:
            runner.log("NOTIFY", f"job {args.job_id} already complete; nothing to resume")
            return 0
        runner.log("STATUS", f"resuming at stage {start} "
                   f"(seed={run['seed_url']}, was status={run['status']})")
        return runner.run(start)
    except KeyboardInterrupt:
        runner.log("NOTIFY", "interrupted by user")
        return 130
    except Exception as e:
        runner.log("NOTIFY", f"runner crashed unexpectedly: {e}")
        traceback.print_exc()
        try:
            runner.store.fail_run(args.job_id, 1, f"resume runner: {e}")
        except Exception:
            pass
        return 1
    finally:
        runner.close()
        if pidfile:
            try:
                pidfile.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            runner.stopfile.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
