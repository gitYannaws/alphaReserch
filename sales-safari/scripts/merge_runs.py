"""Pool several collected runs into ONE corpus run, so themes cluster across subreddits.

Why this exists: each collect writes its own run_id, and every stage from 3 onward is keyed
on run_id. Analysing five subreddits as five runs gives five sets of small themes. Pooling
them first is what lets a pain voiced in one sub cluster with the same pain in another --
which is the only evidence strong enough to trust.

Documents are content-addressed (documents.id is a hash), so the same post collected by two
runs is one row and links once into the merged run.

    python scripts/merge_runs.py --like passport --job-id pbfamily0001
    python scripts/merge_runs.py --runs a1b2,c3d4 --job-id mycorpus --dry-run
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "safari.sqlite"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--like", help="match runs whose seed_url contains this (case-insensitive)")
    ap.add_argument("--runs", help="explicit comma-separated run ids instead of --like")
    ap.add_argument("--job-id", required=True, help="run id to create for the merged corpus")
    ap.add_argument("--seed-label", default="", help="seed_url to record on the merged run")
    ap.add_argument("--min-docs", type=int, default=1,
                    help="skip source runs holding fewer documents than this")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.like and not args.runs:
        sys.exit("pass --like or --runs")

    c = sqlite3.connect(DB)

    if args.runs:
        ids = [x.strip() for x in args.runs.split(",") if x.strip()]
        rows = c.execute(
            "select job_id, seed_url from runs where job_id in (%s)"
            % ",".join("?" * len(ids)), ids).fetchall()
    else:
        rows = c.execute(
            "select job_id, seed_url from runs where lower(seed_url) like ?",
            ("%%%s%%" % args.like.lower(),)).fetchall()

    if not rows:
        sys.exit("no source runs matched")

    # Count first so a tiny or empty run is visible before it is folded in.
    sources = []
    for job_id, seed in rows:
        n = c.execute("select count(*) from run_documents where run_id=?", (job_id,)).fetchone()[0]
        if n >= args.min_docs:
            sources.append((job_id, seed, n))
    sources.sort(key=lambda r: -r[2])

    print("source runs (%d):" % len(sources))
    for job_id, seed, n in sources:
        sub = seed.split("/r/")[-1].split("/")[0] if seed and "/r/" in seed else str(seed)
        print("  %-14s r/%-24s %7d docs" % (job_id, sub[:24], n))

    ph = ",".join("?" * len(sources))
    ids = [s[0] for s in sources]
    unique = c.execute(
        "select count(distinct document_id) from run_documents where run_id in (%s)" % ph,
        ids).fetchone()[0]
    total = sum(s[2] for s in sources)
    print("\n%d links across sources -> %d unique documents (%d duplicates collapse)"
          % (total, unique, total - unique))

    if args.dry_run:
        print("\ndry run, nothing written")
        return

    existing = c.execute("select count(*) from run_documents where run_id=?",
                         (args.job_id,)).fetchone()[0]
    if existing:
        sys.exit("run %s already holds %d documents; pick another --job-id"
                 % (args.job_id, existing))

    now = datetime.now(timezone.utc).isoformat()
    seed_label = args.seed_label or ("merged:%s" % (args.like or "runs"))
    # stage 2 / collected is what the resume runner reads as "ready for extraction".
    c.execute("insert or replace into runs(job_id, seed_url, stage, status, historical, "
              "created_at, updated_at, note) values(?,?,?,?,?,?,?,?)",
              (args.job_id, seed_label, 2, "collected", 1, now, now,
               "merged from %d runs by scripts/merge_runs.py" % len(sources)))
    c.execute("insert or ignore into run_documents(run_id, document_id, collected_at) "
              "select ?, document_id, ? from run_documents where run_id in (%s)" % ph,
              [args.job_id, now] + ids)
    c.commit()

    linked = c.execute("select count(*) from run_documents where run_id=?",
                       (args.job_id,)).fetchone()[0]
    print("\nmerged run %s: %d documents" % (args.job_id, linked))
    print("next: .venv/Scripts/python -m pipeline.resume %s" % args.job_id)
    c.close()


if __name__ == "__main__":
    main()
