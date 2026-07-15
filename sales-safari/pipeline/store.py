"""SQLite store.

Every stage reads prior tables and writes its own outputs. Source documents are
canonical by URL, then linked to each run through run_documents so reruns stay
complete without duplicating raw text.
"""
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

from .collectors.base import Document

_SALT = os.environ.get("SAFARI_SALT", "sales-safari-v1")
SCHEMA_VERSION = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  job_id TEXT PRIMARY KEY,
  seed_url TEXT,
  stage INTEGER,
  status TEXT,
  error TEXT,
  note TEXT,
  use_render INTEGER DEFAULT 0,
  use_firecrawl INTEGER DEFAULT 0,
  use_corpus INTEGER DEFAULT 0,
  historical INTEGER DEFAULT 0,
  search_assist INTEGER DEFAULT 0,
  extractor TEXT,
  extract_provider TEXT,
  extract_model TEXT,
  extract_base_url TEXT,
  extract_config_json TEXT,
  prompt_version TEXT,
  inherited_doc_count INTEGER DEFAULT 0,
  inherited_topic_count INTEGER DEFAULT 0,
  inherited_author_count INTEGER DEFAULT 0,
  last_topic_found_at TEXT,
  updated_at TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  source_type TEXT,
  source_granularity TEXT DEFAULT 'unknown',
  source_url TEXT,
  permalink TEXT,
  title TEXT,
  raw_markdown TEXT,
  author_hash TEXT,
  thread_url TEXT,
  created_at TEXT,
  score INTEGER,
  fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_run ON documents(run_id);

CREATE TABLE IF NOT EXISTS run_documents(
  run_id TEXT,
  document_id TEXT,
  collected_at TEXT,
  PRIMARY KEY(run_id, document_id)
);
CREATE INDEX IF NOT EXISTS idx_run_documents_run ON run_documents(run_id);
CREATE INDEX IF NOT EXISTS idx_run_documents_doc ON run_documents(document_id);

CREATE TABLE IF NOT EXISTS corpora(
  corpus_key TEXT PRIMARY KEY,
  seed_url TEXT,
  created_at TEXT,
  updated_at TEXT,
  backfill_completed_at TEXT
);

CREATE TABLE IF NOT EXISTS corpus_documents(
  corpus_key TEXT,
  document_id TEXT,
  collected_at TEXT,
  PRIMARY KEY(corpus_key, document_id)
);
CREATE INDEX IF NOT EXISTS idx_corpus_documents_key ON corpus_documents(corpus_key);
CREATE INDEX IF NOT EXISTS idx_corpus_documents_doc ON corpus_documents(document_id);

CREATE TABLE IF NOT EXISTS pains(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  document_id TEXT,
  source_id TEXT,
  author_hash TEXT,
  complaint TEXT,
  workflow_pain TEXT,
  workaround TEXT,
  wish TEXT,
  persona TEXT,
  verbatim_span TEXT,
  span_start INTEGER,
  span_end INTEGER,
  source_permalink TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pains_run ON pains(run_id);

CREATE TABLE IF NOT EXISTS embeddings(
  pain_id TEXT PRIMARY KEY,
  run_id TEXT,
  vec BLOB
);

CREATE TABLE IF NOT EXISTS clusters(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  label TEXT,
  size INTEGER,
  distinct_authors INTEGER
);

CREATE TABLE IF NOT EXISTS cluster_members(
  cluster_id TEXT,
  pain_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_clusters_run ON clusters(run_id);

CREATE TABLE IF NOT EXISTS demand_scores(
  cluster_id TEXT PRIMARY KEY,
  run_id TEXT,
  pain_intensity REAL,
  frequency REAL,
  willingness_to_pay REAL,
  reachability REAL,
  recurrence_score REAL,
  demand_score REAL,
  evidence_count INTEGER,
  distinct_authors INTEGER,
  scoring_evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_demand_scores_run ON demand_scores(run_id);

CREATE TABLE IF NOT EXISTS filter_results(
  cluster_id TEXT PRIMARY KEY,
  run_id TEXT,
  dropped INTEGER,
  reasons TEXT
);
CREATE INDEX IF NOT EXISTS idx_filter_results_run ON filter_results(run_id);

CREATE TABLE IF NOT EXISTS soft_filters(
  cluster_id TEXT PRIMARY KEY,
  run_id TEXT,
  solvable TEXT,
  confidence REAL,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_soft_filters_run ON soft_filters(run_id);

CREATE TABLE IF NOT EXISTS competitive_intel(
  cluster_id TEXT PRIMARY KEY,
  run_id TEXT,
  incumbent_count INTEGER,
  saturation_score REAL,
  persistence_score REAL,
  gap_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_competitive_intel_run ON competitive_intel(run_id);

CREATE TABLE IF NOT EXISTS competitors(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  cluster_id TEXT,
  name TEXT,
  url TEXT,
  category TEXT,
  note TEXT,
  review_domain TEXT
);
CREATE INDEX IF NOT EXISTS idx_competitors_run ON competitors(run_id);
CREATE INDEX IF NOT EXISTS idx_competitors_cluster ON competitors(cluster_id);

CREATE TABLE IF NOT EXISTS competitor_reviews(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  competitor_id TEXT,
  app_id TEXT,
  app_name TEXT,
  country TEXT,
  rating INTEGER,
  title TEXT,
  body TEXT,
  author TEXT,
  version TEXT,
  source_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_competitor_reviews_run ON competitor_reviews(run_id);
CREATE INDEX IF NOT EXISTS idx_competitor_reviews_comp ON competitor_reviews(competitor_id);

CREATE TABLE IF NOT EXISTS rankings(
  cluster_id TEXT PRIMARY KEY,
  run_id TEXT,
  rank INTEGER,
  rank_score REAL,
  demand_score REAL,
  persistence_score REAL,
  saturation_score REAL,
  solvable_weight REAL,
  dropped INTEGER,
  filter_reasons TEXT,
  rank_breakdown TEXT
);
CREATE INDEX IF NOT EXISTS idx_rankings_run ON rankings(run_id);

CREATE TABLE IF NOT EXISTS ideas(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  cluster_id TEXT,
  title TEXT,
  pitch TEXT,
  evidence_permalink TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ideas_run ON ideas(run_id);

CREATE TABLE IF NOT EXISTS validation_plans(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  idea_id TEXT,
  kill_test TEXT,
  metric TEXT,
  threshold TEXT,
  timeframe TEXT,
  channel TEXT
);
CREATE INDEX IF NOT EXISTS idx_validation_plans_run ON validation_plans(run_id);

CREATE TABLE IF NOT EXISTS reports(
  run_id TEXT PRIMARY KEY,
  path TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS run_progress(
  run_id TEXT,
  stage INTEGER,
  done INTEGER,
  total INTEGER,
  unit TEXT,
  updated_at TEXT,
  PRIMARY KEY(run_id, stage)
);

CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL UNIQUE,
  label TEXT,
  corpus_key TEXT,
  added_at TEXT,
  last_queued_at TEXT
);
"""


def _doc_id(d: Document) -> str:
    return hashlib.sha1(d.source_url.encode("utf-8")).hexdigest()[:16]


def _hash_author(name):
    if not name:
        return None
    return hashlib.sha1(f"{_SALT}::{name}".encode("utf-8")).hexdigest()[:16]


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn, table: str, column: str, ddl: str):
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate(conn):
    conn.executescript(SCHEMA)
    _ensure_column(conn, "documents", "source_granularity",
                   "source_granularity TEXT DEFAULT 'unknown'")
    _ensure_column(conn, "documents", "score", "score INTEGER")
    _ensure_column(conn, "pains", "span_start", "span_start INTEGER")
    _ensure_column(conn, "pains", "span_end", "span_end INTEGER")
    _ensure_column(conn, "pains", "source_permalink", "source_permalink TEXT")
    _ensure_column(conn, "pains", "persona_canonical", "persona_canonical TEXT")
    _ensure_column(conn, "demand_scores", "recurrence_score", "recurrence_score REAL")
    _ensure_column(conn, "demand_scores", "scoring_evidence", "scoring_evidence TEXT")
    _ensure_column(conn, "rankings", "solvable_weight", "solvable_weight REAL")
    _ensure_column(conn, "rankings", "rank_breakdown", "rank_breakdown TEXT")
    _ensure_column(conn, "runs", "error", "error TEXT")
    _ensure_column(conn, "runs", "note", "note TEXT")
    _ensure_column(conn, "runs", "use_render", "use_render INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "use_firecrawl", "use_firecrawl INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "use_corpus", "use_corpus INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "historical", "historical INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "search_assist", "search_assist INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "extractor", "extractor TEXT")
    _ensure_column(conn, "runs", "extract_provider", "extract_provider TEXT")
    _ensure_column(conn, "runs", "extract_model", "extract_model TEXT")
    _ensure_column(conn, "runs", "extract_base_url", "extract_base_url TEXT")
    _ensure_column(conn, "runs", "extract_config_json", "extract_config_json TEXT")
    _ensure_column(conn, "runs", "prompt_version", "prompt_version TEXT")
    _ensure_column(conn, "runs", "inherited_doc_count", "inherited_doc_count INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "inherited_topic_count", "inherited_topic_count INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "inherited_author_count", "inherited_author_count INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "last_topic_found_at", "last_topic_found_at TEXT")
    _ensure_column(conn, "runs", "updated_at", "updated_at TEXT")
    _ensure_column(conn, "corpora", "backfill_completed_at", "backfill_completed_at TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO run_documents(run_id, document_id, collected_at) "
        "SELECT run_id, id, fetched_at FROM documents WHERE run_id IS NOT NULL"
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


class Store:
    def __init__(self, path: str = "db/safari.sqlite"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys=ON")
        _migrate(self.conn)

    def start_run(self, run_id: str, seed_url: str, use_render: bool = False,
                  use_firecrawl: bool = False, use_corpus: bool = False,
                  extractor: str | None = None, historical: bool = False,
                  search_assist: bool = False, extract_provider: str | None = None,
                  extract_model: str | None = None, extract_base_url: str | None = None,
                  extract_config_json: str | None = None,
                  prompt_version: str | None = None):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO runs("
            "job_id,seed_url,stage,status,error,note,use_render,use_firecrawl,use_corpus,historical,search_assist,extractor,"
            "extract_provider,extract_model,extract_base_url,extract_config_json,prompt_version,"
            "inherited_doc_count,inherited_topic_count,inherited_author_count,last_topic_found_at,updated_at,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, seed_url, 1, "collecting", None, None, int(bool(use_render)),
             int(bool(use_firecrawl)), int(bool(use_corpus)), int(bool(historical)),
             int(bool(search_assist)), extractor, extract_provider, extract_model,
             extract_base_url, extract_config_json, prompt_version, 0, 0, 0, None, now, now))
        self.conn.commit()

    def set_run_inherited_counts(self, run_id: str, docs: int, topics: int, authors: int):
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE runs SET inherited_doc_count=?, inherited_topic_count=?, "
            "inherited_author_count=?, updated_at=? WHERE job_id=?",
            (int(docs or 0), int(topics or 0), int(authors or 0),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        self.conn.commit()

    def set_last_topic_found_at(self, run_id: str, found_at: str):
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE runs SET last_topic_found_at=?, updated_at=? WHERE job_id=?",
            (found_at, datetime.now(timezone.utc).isoformat(), run_id),
        )
        self.conn.commit()

    def set_stage(self, run_id: str, stage: int, status: str):
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE runs SET stage=?, status=?, error=NULL, note=NULL, updated_at=? WHERE job_id=?",
            (stage, status, datetime.now(timezone.utc).isoformat(), run_id))
        self.conn.commit()

    def set_run_note(self, run_id: str, note: str | None):
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE runs SET note=?, updated_at=? WHERE job_id=?",
            (note, datetime.now(timezone.utc).isoformat(), run_id))
        self.conn.commit()

    def set_progress(self, run_id: str, stage: int, done: int, total: int, unit: str = ""):
        """Persist fine-grained within-stage progress (e.g. extract docs done/total),
        so both the live worker and the resume runner drive the same GUI bar."""
        from datetime import datetime, timezone
        self.conn.execute(
            "INSERT OR REPLACE INTO run_progress(run_id,stage,done,total,unit,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, stage, int(done or 0), int(total or 0), unit,
             datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def touch_run(self, run_id: str):
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE runs SET updated_at=? WHERE job_id=?",
            (datetime.now(timezone.utc).isoformat(), run_id))
        self.conn.commit()

    def get_progress(self, run_id: str):
        """Most recently updated progress row for a run, as a dict with a computed
        pct, or None. Frontend renders a bar from done/total (total 0 = indeterminate)."""
        row = self.conn.execute(
            "SELECT stage,done,total,unit FROM run_progress WHERE run_id=? "
            "ORDER BY updated_at DESC LIMIT 1", (run_id,)).fetchone()
        if not row:
            return None
        stage, done, total, unit = row
        pct = round(100 * done / total) if total else None
        return {"stage": stage, "done": done, "total": total, "unit": unit, "pct": pct}

    def fail_run(self, run_id: str, stage: int, message: str):
        """Persist a terminal error so `runs` reflects truth even after JOBS (in-memory) is gone."""
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE runs SET stage=?, status='error', error=?, updated_at=? WHERE job_id=?",
            (stage, message, datetime.now(timezone.utc).isoformat(), run_id))
        self.conn.commit()

    def cancel_run(self, run_id: str, stage: int, message: str = "Stopped by user"):
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE runs SET stage=?, status='cancelled', error=?, updated_at=? WHERE job_id=?",
            (stage, message, datetime.now(timezone.utc).isoformat(), run_id))
        self.conn.commit()

    def get_run(self, run_id: str):
        cols = ["job_id", "seed_url", "stage", "status", "error", "note", "use_render",
                "use_firecrawl", "use_corpus", "historical", "search_assist", "extractor",
                "extract_provider", "extract_model", "extract_base_url", "extract_config_json",
                "prompt_version", "inherited_doc_count",
                "inherited_topic_count", "inherited_author_count", "last_topic_found_at",
                "updated_at", "created_at"]
        row = self.conn.execute(f"SELECT {','.join(cols)} FROM runs WHERE job_id=?", (run_id,)).fetchone()
        return dict(zip(cols, row)) if row else None

    def get_runs_by_status(self, statuses: list[str]):
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        cols = ["job_id", "seed_url", "stage", "status", "error", "note", "use_render",
                "use_firecrawl", "use_corpus", "historical", "search_assist", "extractor",
                "extract_provider", "extract_model", "extract_base_url", "extract_config_json",
                "prompt_version", "inherited_doc_count",
                "inherited_topic_count", "inherited_author_count", "last_topic_found_at",
                "updated_at", "created_at"]
        rows = self.conn.execute(
            f"SELECT {','.join(cols)} FROM runs WHERE status IN ({placeholders}) "
            "ORDER BY COALESCE(updated_at, created_at) DESC",
            tuple(statuses),
        ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def list_runs(self, limit: int = 50, offset: int = 0, q: str = "", status: str = ""):
        where, params = [], []
        if q:
            where.append("(r.seed_url LIKE ? OR r.job_id LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if status:
            where.append("r.status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.conn.execute(
            "SELECT r.job_id, r.seed_url, r.stage, r.status, r.error, r.note, r.created_at, r.updated_at, "
            "r.extractor, r.extract_provider, r.extract_model, r.prompt_version, "
            "MAX(0, (SELECT COUNT(*) FROM run_documents rd WHERE rd.run_id=r.job_id) - "
            "CASE WHEN COALESCE(r.use_corpus,0)=1 THEN COALESCE(r.inherited_doc_count,0) ELSE 0 END) AS doc_count, "
            "(SELECT COUNT(*) FROM pains p WHERE p.run_id=r.job_id) AS pain_count, "
            "(SELECT COUNT(*) FROM clusters c WHERE c.run_id=r.job_id) AS cluster_count, "
            "(SELECT COUNT(*) FROM ideas i WHERE i.run_id=r.job_id) AS idea_count, "
            "(SELECT path FROM reports rep WHERE rep.run_id=r.job_id) AS report_path "
            f"FROM runs r {clause} "
            "ORDER BY COALESCE(r.updated_at, r.created_at) DESC "
            "LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        cols = ["job_id", "seed_url", "stage", "status", "error", "note", "created_at", "updated_at",
                "extractor", "extract_provider", "extract_model", "prompt_version",
                "doc_count", "pain_count", "cluster_count", "idea_count", "report_path"]
        return [dict(zip(cols, row)) for row in rows]

    def count_runs(self, q: str = "", status: str = "") -> int:
        where, params = [], []
        if q:
            where.append("(seed_url LIKE ? OR job_id LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if status:
            where.append("status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self.conn.execute(f"SELECT COUNT(*) FROM runs {clause}", params).fetchone()[0]

    def mark_runs_interrupted(self, statuses: list[str], message: str) -> int:
        from datetime import datetime, timezone
        if not statuses:
            return 0
        placeholders = ",".join("?" for _ in statuses)
        cur = self.conn.execute(
            f"UPDATE runs SET status='interrupted', error=?, updated_at=? "
            f"WHERE status IN ({placeholders})",
            (message, datetime.now(timezone.utc).isoformat(), *statuses),
        )
        self.conn.commit()
        return cur.rowcount or 0

    def set_run_status(self, run_id: str, stage: int, status: str, error: str | None = None):
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE runs SET stage=?, status=?, error=?, note=NULL, updated_at=? WHERE job_id=?",
            (stage, status, error, datetime.now(timezone.utc).isoformat(), run_id),
        )
        self.conn.commit()

    def delete_run(self, run_id: str) -> dict:
        """Delete a run and every row it owns. Returns {'report_path': ...} so the
        caller can unlink the report file. Documents are canonical/shared across runs,
        so we drop this run's links and then garbage-collect only orphaned documents."""
        report = self.conn.execute(
            "SELECT path FROM reports WHERE run_id=?", (run_id,)).fetchone()
        report_path = report[0] if report else None
        c = self.conn
        # cluster_members is keyed by cluster_id, not run_id.
        c.execute(
            "DELETE FROM cluster_members WHERE cluster_id IN "
            "(SELECT id FROM clusters WHERE run_id=?)", (run_id,))
        for tbl in ("pains", "embeddings", "clusters", "demand_scores",
                    "filter_results", "soft_filters", "competitive_intel",
                    "competitors", "competitor_reviews", "rankings", "ideas",
                    "validation_plans", "reports", "run_documents"):
            c.execute(f"DELETE FROM {tbl} WHERE run_id=?", (run_id,))
        # Orphan documents = no run_documents or corpus_documents link left.
        c.execute(
            "DELETE FROM documents WHERE id NOT IN "
            "(SELECT document_id FROM run_documents "
            "UNION SELECT document_id FROM corpus_documents)")
        c.execute("DELETE FROM runs WHERE job_id=?", (run_id,))
        c.commit()
        return {"report_path": report_path}

    def upsert_document(self, run_id: str, d: Document) -> bool:
        from datetime import datetime, timezone
        did = _doc_id(d)
        author_hash = _hash_author(d.author)
        if self.conn.execute("SELECT 1 FROM documents WHERE id=?", (did,)).fetchone():
            self.conn.execute(
                "UPDATE documents SET source_granularity=?, permalink=?, title=?, "
                "raw_markdown=?, author_hash=?, thread_url=?, created_at=?, score=?, fetched_at=? "
                "WHERE id=?",
                (d.source_granularity, d.permalink, d.title, d.raw_markdown,
                 author_hash, d.thread_url, d.created_at, d.score, d.fetched_at, did))
        else:
            self.conn.execute(
                "INSERT INTO documents(id,run_id,source_type,source_granularity,source_url,"
                "permalink,title,raw_markdown,author_hash,thread_url,created_at,score,fetched_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, run_id, d.source_type, d.source_granularity, d.source_url,
                 d.permalink, d.title, d.raw_markdown, author_hash,
                 d.thread_url, d.created_at, d.score, d.fetched_at))

        cur = self.conn.execute(
            "INSERT OR IGNORE INTO run_documents(run_id,document_id,collected_at) VALUES(?,?,?)",
            (run_id, did, d.fetched_at))
        self.conn.execute(
            "UPDATE runs SET updated_at=? WHERE job_id=?",
            (datetime.now(timezone.utc).isoformat(), run_id))
        self.conn.commit()
        return cur.rowcount > 0

    def ensure_corpus(self, corpus_key: str, seed_url: str):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO corpora(corpus_key,seed_url,created_at,updated_at,backfill_completed_at) "
            "VALUES(?,?,?,?,NULL)",
            (corpus_key, seed_url, now, now))
        self.conn.execute(
            "UPDATE corpora SET seed_url=?, updated_at=? WHERE corpus_key=?",
            (seed_url, now, corpus_key))
        self.conn.commit()

    def get_corpus(self, corpus_key: str):
        row = self.conn.execute(
            "SELECT corpus_key,seed_url,created_at,updated_at,backfill_completed_at "
            "FROM corpora WHERE corpus_key=?",
            (corpus_key,),
        ).fetchone()
        if not row:
            return None
        cols = ["corpus_key", "seed_url", "created_at", "updated_at", "backfill_completed_at"]
        return dict(zip(cols, row))

    def mark_corpus_backfilled(self, corpus_key: str):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE corpora SET backfill_completed_at=?, updated_at=? WHERE corpus_key=?",
            (now, now, corpus_key))
        self.conn.commit()

    def link_document_to_corpus(self, corpus_key: str, document_id: str, collected_at: str = None) -> bool:
        from datetime import datetime, timezone
        ts = collected_at or datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO corpus_documents(corpus_key,document_id,collected_at) VALUES(?,?,?)",
            (corpus_key, document_id, ts))
        self.conn.execute(
            "UPDATE corpora SET updated_at=? WHERE corpus_key=?",
            (ts, corpus_key))
        self.conn.commit()
        return cur.rowcount > 0

    def link_run_to_corpus(self, run_id: str, corpus_key: str) -> int:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO run_documents(run_id,document_id,collected_at) "
            "SELECT ?, cd.document_id, COALESCE(cd.collected_at, ?) "
            "FROM corpus_documents cd WHERE cd.corpus_key=?",
            (run_id, ts, corpus_key))
        self.conn.commit()
        return cur.rowcount or 0

    def count_corpus_documents(self, corpus_key: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM corpus_documents WHERE corpus_key=?",
            (corpus_key,)).fetchone()[0]

    def get_corpus_freshness(self, corpus_key: str, stale_days: float = 14.0,
                             low_yield_ratio: float = 0.005, low_yield_floor: int = 5,
                             mid_yield_ratio: float = 0.05) -> dict:
        """How caught-up a corpus is, derived from data already on hand -- no new tracking.
        Finds the most recent run that touched this corpus (via run_documents joined
        through corpus_documents) and compares its `inherited_doc_count` baseline (corpus
        size when that run started) against the corpus's current size. A run that barely
        grew the corpus means the subreddit/forum is largely already collected.

        Returns a dict with doc_count, last_run_at, last_run_new_docs, days_since_last_run,
        backfill_completed_at, and a `status` in:
          "no_data"     - nothing collected yet
          "backfilling" - initial backfill hasn't completed
          "up_to_date"  - last run added ~nothing relative to corpus size; recently run
          "stale"       - was caught up but hasn't run in a while; new content may exist
          "growing"     - last run added a meaningful share; keep collecting
        """
        from datetime import datetime, timezone
        doc_count = self.count_corpus_documents(corpus_key)
        corpus = self.get_corpus(corpus_key) or {}
        backfill_completed_at = corpus.get("backfill_completed_at")
        if doc_count == 0:
            return {"doc_count": 0, "last_run_at": None, "last_run_new_docs": None,
                    "days_since_last_run": None, "backfill_completed_at": backfill_completed_at,
                    "status": "no_data"}

        row = self.conn.execute(
            "SELECT r.job_id, r.created_at, r.inherited_doc_count "
            "FROM runs r "
            "JOIN run_documents rd ON rd.run_id=r.job_id "
            "JOIN corpus_documents cd ON cd.document_id=rd.document_id "
            "WHERE cd.corpus_key=? "
            "GROUP BY r.job_id, r.created_at, r.inherited_doc_count "
            "ORDER BY r.created_at DESC LIMIT 1",
            (corpus_key,),
        ).fetchone()

        if not backfill_completed_at:
            last_run_at = row[1] if row else None
            return {"doc_count": doc_count, "last_run_at": last_run_at, "last_run_new_docs": None,
                    "days_since_last_run": None, "backfill_completed_at": None,
                    "status": "backfilling"}

        last_run_at = row[1] if row else backfill_completed_at
        inherited = row[2] if row else 0
        new_docs = max(0, doc_count - (inherited or 0))
        days_since = None
        try:
            dt = datetime.fromisoformat(last_run_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_since = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            pass

        yield_ratio = new_docs / doc_count
        low_yield = new_docs <= low_yield_floor or yield_ratio <= low_yield_ratio
        if low_yield:
            status = "stale" if (days_since is not None and days_since > stale_days) else "up_to_date"
        elif yield_ratio <= mid_yield_ratio:
            status = "up_to_date" if (days_since is not None and days_since <= stale_days) else "stale"
        else:
            status = "growing"

        return {"doc_count": doc_count, "last_run_at": last_run_at, "last_run_new_docs": new_docs,
                "days_since_last_run": days_since, "backfill_completed_at": backfill_completed_at,
                "status": status}

    def get_corpus_thread_urls(self, corpus_key: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT d.thread_url "
            "FROM corpus_documents cd "
            "JOIN documents d ON d.id=cd.document_id "
            "WHERE cd.corpus_key=? AND d.thread_url IS NOT NULL AND TRIM(d.thread_url)<>''",
            (corpus_key,),
        ).fetchall()
        return {r[0] for r in rows if r and r[0]}

    def list_corpora(self, prefix: str = ""):
        where = "WHERE c.corpus_key LIKE ?" if prefix else ""
        params = [f"{prefix}%"] if prefix else []
        rows = self.conn.execute(
            "SELECT c.corpus_key,c.seed_url,c.created_at,c.updated_at,c.backfill_completed_at,"
            "COUNT(cd.document_id) AS doc_count,"
            "COUNT(DISTINCT COALESCE(NULLIF(d.thread_url,''), NULLIF(d.title,''), d.id)) AS thread_count,"
            "COUNT(DISTINCT d.author_hash) AS author_count,"
            "COALESCE(SUM("
            "LENGTH(CAST(COALESCE(d.raw_markdown,'') AS BLOB)) + "
            "LENGTH(CAST(COALESCE(d.title,'') AS BLOB)) + "
            "LENGTH(CAST(COALESCE(d.source_url,'') AS BLOB)) + "
            "LENGTH(CAST(COALESCE(d.permalink,'') AS BLOB))"
            "), 0) AS total_bytes,"
            "MIN(d.created_at) AS earliest_post_at,"
            "MAX(d.created_at) AS latest_post_at,"
            "MAX(d.fetched_at) AS last_fetched_at "
            "FROM corpora c "
            "LEFT JOIN corpus_documents cd ON cd.corpus_key=c.corpus_key "
            "LEFT JOIN documents d ON d.id=cd.document_id "
            f"{where} "
            "GROUP BY c.corpus_key,c.seed_url,c.created_at,c.updated_at,c.backfill_completed_at "
            "ORDER BY COALESCE(c.updated_at,c.created_at) DESC",
            params,
        ).fetchall()
        cols = ["corpus_key", "seed_url", "created_at", "updated_at", "backfill_completed_at", "doc_count",
                "thread_count", "author_count", "total_bytes", "earliest_post_at", "latest_post_at",
                "last_fetched_at"]
        return [dict(zip(cols, row)) for row in rows]

    def add_source(self, source_id: str, url: str, label: str, corpus_key: str | None) -> bool:
        """Identity is corpus_key (the actual forum/subreddit), not the raw seed url --
        different sort/query-param variants of the same subreddit (e.g. /new/ vs
        ?t=all) normalize to the same corpus_key and must not create duplicate rows."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        if corpus_key:
            existing = self.conn.execute(
                "SELECT id FROM sources WHERE corpus_key=?", (corpus_key,)).fetchone()
            if existing:
                return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO sources(id,url,label,corpus_key,added_at,last_queued_at) "
            "VALUES(?,?,?,?,?,NULL)",
            (source_id, url, label, corpus_key, now))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_source(self, source_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def mark_source_queued(self, source_id: str):
        from datetime import datetime, timezone
        self.conn.execute(
            "UPDATE sources SET last_queued_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), source_id))
        self.conn.commit()

    def list_sources(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.id,s.url,s.label,s.corpus_key,s.added_at,s.last_queued_at,"
            "COUNT(cd.document_id) AS doc_count,"
            "COUNT(DISTINCT COALESCE(NULLIF(d.thread_url,''), NULLIF(d.title,''), d.id)) AS thread_count,"
            "COUNT(DISTINCT d.author_hash) AS author_count,"
            "COALESCE(SUM("
            "LENGTH(CAST(COALESCE(d.raw_markdown,'') AS BLOB)) + "
            "LENGTH(CAST(COALESCE(d.title,'') AS BLOB)) + "
            "LENGTH(CAST(COALESCE(d.source_url,'') AS BLOB)) + "
            "LENGTH(CAST(COALESCE(d.permalink,'') AS BLOB))"
            "), 0) AS total_bytes,"
            "MIN(d.created_at) AS earliest_post_at,"
            "MAX(d.created_at) AS latest_post_at,"
            "MAX(d.fetched_at) AS last_fetched_at "
            "FROM sources s "
            "LEFT JOIN corpus_documents cd ON cd.corpus_key=s.corpus_key "
            "LEFT JOIN documents d ON d.id=cd.document_id "
            "GROUP BY s.id,s.url,s.label,s.corpus_key,s.added_at,s.last_queued_at "
            "ORDER BY s.added_at DESC"
        ).fetchall()
        cols = ["id", "url", "label", "corpus_key", "added_at", "last_queued_at",
                "doc_count", "thread_count", "author_count", "total_bytes",
                "earliest_post_at", "latest_post_at", "last_fetched_at"]
        out = [dict(zip(cols, row)) for row in rows]
        for src in out:
            fresh = (self.get_corpus_freshness(src["corpus_key"]) if src["corpus_key"]
                     else {"status": "no_data", "last_run_new_docs": None,
                           "days_since_last_run": None, "backfill_completed_at": None})
            src["freshness"] = fresh["status"]
            src["last_run_new_docs"] = fresh["last_run_new_docs"]
            src["days_since_last_run"] = fresh["days_since_last_run"]
        return out

    def backfill_sources_from_corpora(self) -> int:
        """Create a Source row for every corpus that doesn't already have one, so corpora
        collected before the sources table existed still appear on the merged Sources page.
        Idempotent -- only inserts the missing ones."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        rows = self.conn.execute(
            "SELECT c.corpus_key, c.seed_url FROM corpora c "
            "LEFT JOIN sources s ON s.corpus_key=c.corpus_key WHERE s.id IS NULL"
        ).fetchall()
        n = 0
        for corpus_key, seed_url in rows:
            self.conn.execute(
                "INSERT OR IGNORE INTO sources(id,url,label,corpus_key,added_at,last_queued_at) "
                "VALUES(?,?,?,?,?,NULL)",
                (uuid.uuid4().hex[:12], seed_url or corpus_key, None, corpus_key, now))
            n += 1
        self.conn.commit()
        return n

    def get_source(self, source_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id,url,label,corpus_key,added_at,last_queued_at FROM sources WHERE id=?",
            (source_id,)).fetchone()
        if not row:
            return None
        cols = ["id", "url", "label", "corpus_key", "added_at", "last_queued_at"]
        return dict(zip(cols, row))

    def get_document_id_by_source_url(self, source_url: str) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM documents WHERE source_url=?",
            (source_url,)).fetchone()
        return row[0] if row else None

    def count_documents(self, run_id: str = None) -> int:
        if run_id:
            return self.conn.execute(
                "SELECT COUNT(*) FROM run_documents WHERE run_id=?",
                (run_id,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def count_distinct_authors(self, run_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT d.author_hash) FROM run_documents rd "
            "JOIN documents d ON d.id=rd.document_id "
            "WHERE rd.run_id=? AND d.author_hash IS NOT NULL",
            (run_id,)).fetchone()[0]

    def count_topics(self, run_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(d.thread_url, ''), NULLIF(d.title, ''), d.id)) "
            "FROM run_documents rd "
            "JOIN documents d ON d.id=rd.document_id "
            "WHERE rd.run_id=?",
            (run_id,),
        ).fetchone()[0]

    def get_run_display_counts(self, run_id: str, run: dict | None = None) -> dict:
        run = run or self.get_run(run_id) or {}
        docs = self.count_documents(run_id)
        topics = self.count_topics(run_id)
        authors = self.count_distinct_authors(run_id)
        if run.get("use_corpus"):
            docs = max(0, docs - int(run.get("inherited_doc_count") or 0))
            topics = max(0, topics - int(run.get("inherited_topic_count") or 0))
            authors = max(0, authors - int(run.get("inherited_author_count") or 0))
        return {"new": docs, "threads": topics, "authors": authors}

    def run_has_topic(self, run_id: str, topic_key: str) -> bool:
        if not topic_key:
            return False
        row = self.conn.execute(
            "SELECT 1 "
            "FROM run_documents rd "
            "JOIN documents d ON d.id=rd.document_id "
            "WHERE rd.run_id=? "
            "AND COALESCE(NULLIF(d.thread_url, ''), NULLIF(d.title, ''), d.id)=? "
            "LIMIT 1",
            (run_id, topic_key),
        ).fetchone()
        return bool(row)

    def get_last_topic_found_at(self, run_id: str, run: dict | None = None) -> str | None:
        run = run or self.get_run(run_id) or {}
        if run.get("last_topic_found_at"):
            return run["last_topic_found_at"]
        inherited_cutoff = None
        if run.get("use_corpus") and run.get("inherited_topic_count"):
            inherited_cutoff = run.get("created_at")
        params = [run_id]
        sql = (
            "SELECT MAX(first_seen) FROM ("
            "SELECT MIN(rd.collected_at) AS first_seen "
            "FROM run_documents rd "
            "JOIN documents d ON d.id=rd.document_id "
            "WHERE rd.run_id=? "
        )
        if inherited_cutoff:
            sql += "AND rd.collected_at > ? "
            params.append(inherited_cutoff)
        sql += (
            "GROUP BY COALESCE(NULLIF(d.thread_url, ''), NULLIF(d.title, ''), d.id)"
            ")"
        )
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row and row[0] else None

    # ---- stage 3: pains ----
    def get_documents(self, run_id: str):
        cols = ["id", "source_url", "permalink", "title", "raw_markdown",
                "author_hash", "source_granularity"]
        rows = self.conn.execute(
            "SELECT d.id,d.source_url,d.permalink,d.title,d.raw_markdown,"
            "d.author_hash,d.source_granularity FROM run_documents rd "
            "JOIN documents d ON d.id=rd.document_id WHERE rd.run_id=?",
            (run_id,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def insert_pain(self, run_id: str, p: dict) -> bool:
        pid = hashlib.sha1(
            f"{run_id}::{p['document_id']}::{p['verbatim_span']}".encode("utf-8")).hexdigest()[:16]
        if self.conn.execute("SELECT 1 FROM pains WHERE id=?", (pid,)).fetchone():
            return False
        from datetime import datetime, timezone
        self.conn.execute(
            "INSERT INTO pains(id,run_id,document_id,source_id,author_hash,complaint,"
            "workflow_pain,workaround,wish,persona,verbatim_span,span_start,span_end,"
            "source_permalink,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, run_id, p["document_id"], p.get("source_id"), p.get("author_hash"),
             p.get("complaint", ""), p.get("workflow_pain", ""), p.get("workaround", ""),
             p.get("wish", ""), p.get("persona", ""), p["verbatim_span"],
             p.get("span_start"), p.get("span_end"), p.get("source_permalink"),
             datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        return True

    def count_pains(self, run_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM pains WHERE run_id=?", (run_id,)).fetchone()[0]

    def distinct_personas(self, run_id: str):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT persona FROM pains WHERE run_id=? "
            "AND persona IS NOT NULL AND TRIM(persona)<>''", (run_id,)).fetchall()]

    def update_persona_canonical(self, run_id: str, mapping: dict):
        """mapping: raw persona string -> canonical segment label."""
        for raw, canon in mapping.items():
            self.conn.execute(
                "UPDATE pains SET persona_canonical=? WHERE run_id=? AND persona=?",
                (canon, run_id, raw))
        self.conn.commit()

    def get_pains(self, run_id: str):
        cols = ["id", "complaint", "workflow_pain", "wish", "verbatim_span",
                "span_start", "span_end", "source_permalink", "author_hash", "persona"]
        rows = self.conn.execute(
            f"SELECT {','.join(cols)} FROM pains WHERE run_id=?", (run_id,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def get_document_rows(self, run_id: str, limit: int = 60):
        rows = self.conn.execute(
            "SELECT d.title, d.author_hash, substr(d.raw_markdown,1,180), d.permalink, "
            "d.source_granularity FROM run_documents rd "
            "JOIN documents d ON d.id=rd.document_id "
            "WHERE rd.run_id=? LIMIT ?", (run_id, limit)).fetchall()
        return [{"title": r[0], "author": r[1], "snippet": r[2],
                 "permalink": r[3], "source_granularity": r[4]} for r in rows]

    # ---- stage 4: embeddings ----
    def save_embedding(self, run_id: str, pain_id: str, blob: bytes):
        self.conn.execute("INSERT OR REPLACE INTO embeddings(pain_id,run_id,vec) VALUES(?,?,?)",
                          (pain_id, run_id, blob))
        self.conn.commit()

    def get_embeddings(self, run_id: str):
        rows = self.conn.execute(
            "SELECT e.pain_id, e.vec, p.author_hash FROM embeddings e "
            "JOIN pains p ON p.id=e.pain_id WHERE e.run_id=?", (run_id,)).fetchall()
        return rows  # [(pain_id, vec_bytes, author_hash)]

    # ---- stage 5: clusters ----
    def clear_clusters(self, run_id: str):
        ids = [r[0] for r in self.conn.execute("SELECT id FROM clusters WHERE run_id=?", (run_id,)).fetchall()]
        self.conn.execute("DELETE FROM clusters WHERE run_id=?", (run_id,))
        for cid in ids:
            self.conn.execute("DELETE FROM cluster_members WHERE cluster_id=?", (cid,))
        self.conn.commit()

    def save_cluster(self, cid: str, run_id: str, label: str, size: int, distinct_authors: int, pain_ids):
        self.conn.execute("INSERT OR REPLACE INTO clusters(id,run_id,label,size,distinct_authors) VALUES(?,?,?,?,?)",
                          (cid, run_id, label, size, distinct_authors))
        for pid in pain_ids:
            self.conn.execute("INSERT INTO cluster_members(cluster_id,pain_id) VALUES(?,?)", (cid, pid))
        self.conn.commit()

    def representative_text(self, pain_ids):
        q = "SELECT complaint, workflow_pain, wish, verbatim_span FROM pains WHERE id IN (%s)" % \
            ",".join("?" * len(pain_ids))
        bad = {"1", ".", "-", "n/a", "na", "none", "null", "unknown"}
        for row in self.conn.execute(q, pain_ids).fetchall():
            for field in row:
                if not field:
                    continue
                text = field.strip()
                if not text or text.lower() in bad or len(text) < 12:
                    continue
                return text
        return "(cluster)"

    def get_clusters(self, run_id: str):
        rows = self.conn.execute(
            "SELECT label, size, distinct_authors FROM clusters WHERE run_id=? "
            "ORDER BY distinct_authors DESC, size DESC", (run_id,)).fetchall()
        return [{"label": r[0], "size": r[1], "distinct_authors": r[2]} for r in rows]

    def get_cluster_details(self, run_id: str):
        rows = self.conn.execute(
            "SELECT c.id,c.label,c.size,c.distinct_authors,p.id,p.complaint,"
            "p.workflow_pain,p.workaround,p.wish,p.persona,p.verbatim_span,"
            "p.source_permalink,p.author_hash,d.source_granularity,d.score "
            "FROM clusters c "
            "JOIN cluster_members cm ON cm.cluster_id=c.id "
            "JOIN pains p ON p.id=cm.pain_id "
            "LEFT JOIN documents d ON d.id=p.document_id "
            "WHERE c.run_id=? "
            "ORDER BY c.distinct_authors DESC,c.size DESC,c.id",
            (run_id,)).fetchall()
        clusters = {}
        for r in rows:
            cid = r[0]
            if cid not in clusters:
                clusters[cid] = {
                    "id": cid,
                    "label": r[1],
                    "size": r[2],
                    "distinct_authors": r[3],
                    "pains": [],
                }
            clusters[cid]["pains"].append({
                "id": r[4],
                "complaint": r[5],
                "workflow_pain": r[6],
                "workaround": r[7],
                "wish": r[8],
                "persona": r[9],
                "verbatim_span": r[10],
                "source_permalink": r[11],
                "author_hash": r[12],
                "source_granularity": r[13],
                "score": r[14],
            })
        return list(clusters.values())

    def count_clusters(self, run_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM clusters WHERE run_id=?", (run_id,)).fetchone()[0]

    # ---- stage 6: demand ----
    def clear_demand(self, run_id: str):
        self.conn.execute("DELETE FROM demand_scores WHERE run_id=?", (run_id,))
        self.conn.commit()

    def save_demand_score(self, run_id: str, cluster_id: str, score: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO demand_scores(cluster_id,run_id,pain_intensity,"
            "frequency,willingness_to_pay,reachability,recurrence_score,demand_score,evidence_count,"
            "distinct_authors,scoring_evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cluster_id, run_id, score["pain_intensity"], score["frequency"],
             score["willingness_to_pay"], score["reachability"],
             score.get("recurrence_score", 0), score["demand_score"],
             score["evidence_count"], score["distinct_authors"],
             json.dumps(score.get("scoring_evidence", {}))))
        self.conn.commit()

    # ---- stage 7: filters ----
    def clear_filters(self, run_id: str):
        self.conn.execute("DELETE FROM filter_results WHERE run_id=?", (run_id,))
        self.conn.commit()

    def save_filter_result(self, run_id: str, cluster_id: str, dropped: bool, reasons):
        self.conn.execute(
            "INSERT OR REPLACE INTO filter_results(cluster_id,run_id,dropped,reasons) "
            "VALUES(?,?,?,?)",
            (cluster_id, run_id, 1 if dropped else 0, json.dumps(reasons)))
        self.conn.commit()

    # ---- stage 7.5: soft software-solvability filter ----
    def clear_soft_filters(self, run_id: str):
        self.conn.execute("DELETE FROM soft_filters WHERE run_id=?", (run_id,))
        self.conn.commit()

    def save_soft_filter(self, run_id: str, cluster_id: str, solvable: str,
                         confidence: float, reason: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO soft_filters(cluster_id,run_id,solvable,confidence,reason) "
            "VALUES(?,?,?,?,?)",
            (cluster_id, run_id, solvable, confidence, reason))
        self.conn.commit()

    # ---- stage 9.5: competitor discovery ----
    def clear_competitors(self, run_id: str):
        self.conn.execute("DELETE FROM competitors WHERE run_id=?", (run_id,))
        self.conn.commit()

    def save_competitor(self, run_id: str, cluster_id: str, c: dict):
        cid = hashlib.sha1(
            f"{run_id}::{cluster_id}::{c.get('name','')}".encode("utf-8")).hexdigest()[:16]
        self.conn.execute(
            "INSERT OR REPLACE INTO competitors(id,run_id,cluster_id,name,url,category,"
            "note,review_domain) VALUES(?,?,?,?,?,?,?,?)",
            (cid, run_id, cluster_id, c.get("name", ""), c.get("url", ""),
             c.get("category", ""), c.get("note", ""), c.get("review_domain", "")))
        self.conn.commit()
        return cid

    def get_competitors(self, run_id: str, cluster_id: str = None):
        if cluster_id:
            rows = self.conn.execute(
                "SELECT id,cluster_id,name,url,category,note,review_domain FROM competitors "
                "WHERE run_id=? AND cluster_id=? ORDER BY name", (run_id, cluster_id)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id,cluster_id,name,url,category,note,review_domain FROM competitors "
                "WHERE run_id=? ORDER BY cluster_id,name", (run_id,)).fetchall()
        cols = ["id", "cluster_id", "name", "url", "category", "note", "review_domain"]
        return [dict(zip(cols, r)) for r in rows]

    # ---- stage 9.6: competitor low-star reviews ----
    def clear_reviews(self, run_id: str):
        self.conn.execute("DELETE FROM competitor_reviews WHERE run_id=?", (run_id,))
        self.conn.commit()

    def save_review(self, run_id: str, competitor_id: str, r: dict) -> bool:
        key = r.get("review_id") or f"{r.get('author','')}::{r.get('title','')}"
        rid = hashlib.sha1(
            f"{run_id}::{competitor_id}::{r.get('country','')}::{key}".encode("utf-8")).hexdigest()[:16]
        if self.conn.execute("SELECT 1 FROM competitor_reviews WHERE id=?", (rid,)).fetchone():
            return False
        self.conn.execute(
            "INSERT INTO competitor_reviews(id,run_id,competitor_id,app_id,app_name,country,"
            "rating,title,body,author,version,source_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, run_id, competitor_id, r.get("app_id"), r.get("app_name"), r.get("country"),
             r.get("rating"), r.get("title", ""), r.get("body", ""), r.get("author", ""),
             r.get("version", ""), r.get("source_url", "")))
        self.conn.commit()
        return True

    def get_reviews(self, run_id: str, competitor_id: str = None):
        base = ("SELECT id,competitor_id,app_id,app_name,country,rating,title,body,author,"
                "version,source_url FROM competitor_reviews WHERE run_id=?")
        args = [run_id]
        if competitor_id:
            base += " AND competitor_id=?"
            args.append(competitor_id)
        base += " ORDER BY competitor_id,rating,id"
        rows = self.conn.execute(base, args).fetchall()
        cols = ["id", "competitor_id", "app_id", "app_name", "country", "rating", "title",
                "body", "author", "version", "source_url"]
        return [dict(zip(cols, r)) for r in rows]

    # ---- stage 8: competition ----
    def clear_competition(self, run_id: str):
        self.conn.execute("DELETE FROM competitive_intel WHERE run_id=?", (run_id,))
        self.conn.commit()

    def save_competitive_intel(self, run_id: str, cluster_id: str, intel: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO competitive_intel(cluster_id,run_id,incumbent_count,"
            "saturation_score,persistence_score,gap_summary) VALUES(?,?,?,?,?,?)",
            (cluster_id, run_id, intel["incumbent_count"], intel["saturation_score"],
             intel["persistence_score"], intel["gap_summary"]))
        self.conn.commit()

    def competitor_counts(self, run_id: str) -> dict:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT cluster_id,COUNT(*) FROM competitors WHERE run_id=? GROUP BY cluster_id",
            (run_id,)).fetchall()}

    def set_saturation(self, run_id: str, cluster_id: str, saturation: float,
                       incumbent_count: int = None):
        """Backfill real saturation onto an existing competitive_intel row (from discovered
        competitor counts), so rank reflects competition instead of the inert heuristic."""
        if incumbent_count is None:
            self.conn.execute(
                "UPDATE competitive_intel SET saturation_score=? WHERE run_id=? AND cluster_id=?",
                (saturation, run_id, cluster_id))
        else:
            self.conn.execute(
                "UPDATE competitive_intel SET saturation_score=?, incumbent_count=? "
                "WHERE run_id=? AND cluster_id=?",
                (saturation, incumbent_count, run_id, cluster_id))
        self.conn.commit()

    def get_solvable_map(self, run_id: str) -> dict:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT cluster_id,solvable FROM soft_filters WHERE run_id=?", (run_id,)).fetchall()}

    # ---- stage 9: rank ----
    def clear_rankings(self, run_id: str):
        self.conn.execute("DELETE FROM rankings WHERE run_id=?", (run_id,))
        self.conn.commit()

    def save_ranking(self, run_id: str, row: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO rankings(cluster_id,run_id,rank,rank_score,"
            "demand_score,persistence_score,saturation_score,solvable_weight,"
            "dropped,filter_reasons,rank_breakdown) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (row["cluster_id"], run_id, row["rank"], row["rank_score"],
             row["demand_score"], row["persistence_score"], row["saturation_score"],
             row.get("solvable_weight"), 1 if row["dropped"] else 0,
             json.dumps(row["filter_reasons"]),
             json.dumps(row.get("rank_breakdown", {}))))
        self.conn.commit()

    def get_ranked_clusters(self, run_id: str, include_dropped: bool = False):
        where = "" if include_dropped else "AND r.dropped=0"
        rows = self.conn.execute(
            "SELECT r.rank,r.rank_score,r.demand_score,r.persistence_score,"
            "r.saturation_score,r.dropped,r.filter_reasons,c.id,c.label,c.size,"
            "c.distinct_authors,ci.gap_summary,sf.solvable,sf.confidence,sf.reason,"
            "ds.pain_intensity,ds.frequency,ds.willingness_to_pay,ds.reachability,"
            "ds.recurrence_score,ds.scoring_evidence,r.solvable_weight,r.rank_breakdown "
            "FROM rankings r "
            "JOIN clusters c ON c.id=r.cluster_id "
            "LEFT JOIN competitive_intel ci ON ci.cluster_id=r.cluster_id "
            "LEFT JOIN soft_filters sf ON sf.cluster_id=r.cluster_id "
            "LEFT JOIN demand_scores ds ON ds.cluster_id=r.cluster_id "
            f"WHERE r.run_id=? {where} "
            "ORDER BY r.dropped ASC,r.rank ASC,r.rank_score DESC",
            (run_id,)).fetchall()
        return [{
            "rank": r[0],
            "rank_score": r[1],
            "demand_score": r[2],
            "persistence_score": r[3],
            "saturation_score": r[4],
            "dropped": bool(r[5]),
            "filter_reasons": json.loads(r[6] or "[]"),
            "cluster_id": r[7],
            "label": r[8],
            "size": r[9],
            "distinct_authors": r[10],
            "gap_summary": r[11],
            "solvable": r[12],
            "solvable_confidence": r[13],
            "solvable_reason": r[14],
            "pain_intensity": r[15],
            "frequency": r[16],
            "willingness_to_pay": r[17],
            "reachability": r[18],
            "recurrence_score": r[19],
            "scoring_evidence": json.loads(r[20] or "{}"),
            "solvable_weight": r[21],
            "rank_breakdown": json.loads(r[22] or "{}"),
        } for r in rows]

    # ---- stages 10-11: ideas + validation ----
    def clear_ideas(self, run_id: str):
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM ideas WHERE run_id=?", (run_id,)).fetchall()]
        self.conn.execute("DELETE FROM ideas WHERE run_id=?", (run_id,))
        for iid in ids:
            self.conn.execute("DELETE FROM validation_plans WHERE idea_id=?", (iid,))
        self.conn.commit()

    def save_idea(self, run_id: str, cluster_id: str, title: str, pitch: str,
                  evidence_permalink: str):
        from datetime import datetime, timezone
        iid = hashlib.sha1(f"{run_id}::{cluster_id}::{title}".encode("utf-8")).hexdigest()[:16]
        self.conn.execute(
            "INSERT OR REPLACE INTO ideas(id,run_id,cluster_id,title,pitch,"
            "evidence_permalink,created_at) VALUES(?,?,?,?,?,?,?)",
            (iid, run_id, cluster_id, title, pitch, evidence_permalink,
             datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        return iid

    def get_ideas(self, run_id: str):
        rows = self.conn.execute(
            "SELECT i.id,i.cluster_id,i.title,i.pitch,i.evidence_permalink,"
            "v.kill_test,v.metric,v.threshold,v.timeframe,v.channel "
            "FROM ideas i LEFT JOIN validation_plans v ON v.idea_id=i.id "
            "WHERE i.run_id=? ORDER BY i.created_at",
            (run_id,)).fetchall()
        return [{
            "id": r[0],
            "cluster_id": r[1],
            "title": r[2],
            "pitch": r[3],
            "evidence_permalink": r[4],
            "kill_test": r[5],
            "metric": r[6],
            "threshold": r[7],
            "timeframe": r[8],
            "channel": r[9],
        } for r in rows]

    def save_validation_plan(self, run_id: str, idea_id: str, plan: dict):
        vid = hashlib.sha1(f"{run_id}::{idea_id}".encode("utf-8")).hexdigest()[:16]
        self.conn.execute(
            "INSERT OR REPLACE INTO validation_plans(id,run_id,idea_id,kill_test,"
            "metric,threshold,timeframe,channel) VALUES(?,?,?,?,?,?,?,?)",
            (vid, run_id, idea_id, plan["kill_test"], plan["metric"],
             plan["threshold"], plan["timeframe"], plan["channel"]))
        self.conn.commit()
        return vid

    # ---- stage 12: report ----
    def save_report(self, run_id: str, path: str):
        from datetime import datetime, timezone
        self.conn.execute(
            "INSERT OR REPLACE INTO reports(run_id,path,created_at) VALUES(?,?,?)",
            (run_id, path, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def get_report(self, run_id: str):
        row = self.conn.execute(
            "SELECT path,created_at FROM reports WHERE run_id=?", (run_id,)).fetchone()
        return {"path": row[0], "created_at": row[1]} if row else None

    def close(self):
        self.conn.close()
