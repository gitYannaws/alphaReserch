"""Retire pains whose verbatim span is too short to be evidence, on a FINISHED run.

The stage-3a span gate proves a quote is real. It does not prove the quote says anything.
Run pbfamily0001 stored 2,242 pains whose entire evidence was under 25 characters ("Oh
Lordy.", "over priced", "They love to fuck"); 1,199 of them reached clustering and 219 sat
inside top-100 ranked themes. `config.yaml -> extract.min_span_chars` stops that at the
source for new runs. This is the backfill for runs already extracted.

It does NOT delete anything. It sets `verified = 0`, which `store.get_pains` already
withholds from stages 4-12, so the rows stay readable and the change reverses with
`--restore`. Re-run stages 4 (embed) onward afterwards for the run to reflect it.

    .venv/Scripts/python scripts/prune_short_spans.py <run_id>            # dry run
    .venv/Scripts/python scripts/prune_short_spans.py <run_id> --apply
    .venv/Scripts/python scripts/prune_short_spans.py <run_id> --restore --apply
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

# Only rows this script itself retired carry both marks, so --restore cannot resurrect a
# pain that a real stage-3b judge rejected on its merits.
REASON = "short_span_backfill"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--min-chars", type=int, default=None,
                    help="override config extract.min_span_chars")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    ap.add_argument("--restore", action="store_true",
                    help="undo a previous prune instead of running one")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    floor = args.min_chars if args.min_chars is not None else int(
        (cfg.get("extract") or {}).get("min_span_chars", 25) or 0)
    if not args.restore and floor <= 0:
        print("min_span_chars is 0 - nothing to prune")
        return 0

    conn = sqlite3.connect(str(ROOT / cfg["db_path"]))
    if args.restore:
        where = "run_id=? AND verified=0 AND verify_reason=?"
        params = (args.run_id, REASON)
        set_clause = "verified=NULL, verify_reason=NULL"
    else:
        where = ("run_id=? AND COALESCE(verified,1)!=0 "
                 "AND LENGTH(TRIM(COALESCE(verbatim_span,'')))<?")
        params = (args.run_id, floor)
        set_clause = "verified=0, pain_type='no_pain', verify_reason=?"

    n = conn.execute(f"SELECT COUNT(*) FROM pains WHERE {where}", params).fetchone()[0]
    verb = "restore" if args.restore else f"retire (span < {floor} chars)"
    print(f"{args.run_id}: {n} pains to {verb}")
    if not n:
        return 0

    if not args.restore:
        print("  examples:")
        for (span,) in conn.execute(
                f"SELECT verbatim_span FROM pains WHERE {where} LIMIT 8", params):
            print(f"    {span!r}")
    if not args.apply:
        print("  dry run - pass --apply to write")
        return 0

    write_params = params if args.restore else (REASON, *params)
    conn.execute(f"UPDATE pains SET {set_clause} WHERE {where}", write_params)
    conn.commit()
    print(f"  done. Re-run stages 4-12 for {args.run_id} to pick this up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
