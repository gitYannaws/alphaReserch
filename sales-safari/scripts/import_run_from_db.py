"""Copy one run's corpus out of another safari.sqlite into this one.

Why this exists: the always-on box (`mon`) collects, but the GPU box runs stages 3-12.
Rather than merge whole databases, this lifts exactly one run_id: its `runs` row, its
`documents`, and its `run_documents` links.

`documents.id` is a content hash, so INSERT OR IGNORE dedups by content. A document already
present from another run keeps its original row and only gains a run_documents link.

    python scripts/import_run_from_db.py <source.sqlite> <run_id>
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "db" / "safari.sqlite"

TABLES = ("runs", "documents", "run_documents")


def columns(conn, table):
    return [r[1] for r in conn.execute("pragma table_info(%s)" % table)]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src_path, run_id = sys.argv[1], sys.argv[2]
    if not Path(src_path).exists():
        sys.exit("source database not found: %s" % src_path)

    dest = sqlite3.connect(DEST)
    dest.execute("attach database ? as src", (src_path,))

    # A column added on one box but not the other would silently shift the values, so
    # insert by explicit shared column list rather than SELECT *.
    shared = {}
    for t in TABLES:
        d = columns(dest, t)
        s = [r[1] for r in dest.execute("pragma src.table_info(%s)" % t)]
        shared[t] = [c for c in d if c in s]
        missing = [c for c in d if c not in s]
        if missing:
            print("  note: %s columns absent from source, left null: %s" % (t, missing))

    n_src = dest.execute(
        "select count(*) from src.run_documents where run_id=?", (run_id,)).fetchone()[0]
    if not n_src:
        sys.exit("source holds no run_documents for run %s" % run_id)
    print("source has %d run_documents for run %s" % (n_src, run_id))

    before = dest.execute("select count(*) from documents").fetchone()[0]

    cols = ", ".join(shared["runs"])
    dest.execute(
        "insert or ignore into runs (%s) select %s from src.runs where job_id=?" % (cols, cols),
        (run_id,))

    cols = ", ".join(shared["documents"])
    dest.execute(
        "insert or ignore into documents (%s) select %s from src.documents "
        "where id in (select document_id from src.run_documents where run_id=?)"
        % (cols, cols), (run_id,))

    cols = ", ".join(shared["run_documents"])
    dest.execute(
        "insert or ignore into run_documents (%s) select %s from src.run_documents "
        "where run_id=?" % (cols, cols), (run_id,))

    dest.commit()

    after = dest.execute("select count(*) from documents").fetchone()[0]
    linked = dest.execute(
        "select count(*) from run_documents where run_id=?", (run_id,)).fetchone()[0]
    print("documents %d -> %d (+%d new)" % (before, after, after - before))
    print("run_documents linked to %s: %d" % (run_id, linked))
    print("run row:", dest.execute(
        "select job_id, seed_url, stage, status from runs where job_id=?", (run_id,)).fetchone())
    dest.close()


if __name__ == "__main__":
    main()
