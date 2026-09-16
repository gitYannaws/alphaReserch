"""Live progress of the laptop historical run. Plain connection, not immutable -
the file is being written right now and WAL allows concurrent readers."""
import sqlite3
from datetime import datetime, timezone

con = sqlite3.connect("db/safari.sqlite", timeout=20)
cur = con.cursor()

n = cur.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
print(f"documents: {n:,}")

try:
    lo, hi = cur.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM documents"
    ).fetchone()
    print(f"date range: {lo}  ->  {hi}")
except Exception as exc:
    print("range err:", exc)

print("\nby granularity:")
for row in cur.execute(
    "SELECT source_granularity, COUNT(*) c FROM documents GROUP BY source_granularity ORDER BY c DESC"
):
    print(f"  {row[0]}: {row[1]:,}")

print("\nauthors:", cur.execute(
    "SELECT COUNT(DISTINCT author) FROM documents"
).fetchone()[0])

print("\nruns:")
for row in cur.execute(
    "SELECT job_id, seed_url, stage, status, created_at FROM runs ORDER BY created_at DESC LIMIT 3"
):
    print("  ", row)

print("\nnewest 3 titles:")
for (t,) in cur.execute(
    "SELECT title FROM documents WHERE title != '' ORDER BY rowid DESC LIMIT 3"
):
    print("  ", (t or "")[:75])

con.close()
