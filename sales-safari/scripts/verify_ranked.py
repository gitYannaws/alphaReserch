"""Run the stage-3b judge over the pains that actually reach a ranked theme.

Why a targeted backfill and not the whole run: `docs/single-pass-extraction-2026-08-17.md`
turned verify off and stated the cost plainly -- qwen2.5 measured **22% precision alone
against 60% with verification** -- so roughly three noise candidates per four survive into
clustering. Judging all 72,610 of run pbfamily0001's pains means ~7,300 Claude calls. Judging
only the pains inside RANKED themes is the slice that becomes an idea, and it is the slice
whose precision anyone acts on.

`verify_run` consumes `store.get_unverified_pains`, which selects `verified IS NULL`.
Candidates written while `require_verdict: false` carry `verified = 1`, so this script resets
the target slice to NULL first and lets the judge see exactly that slice, nothing else.

    .venv/Scripts/python scripts/verify_ranked.py <run_id> --sample 300   # measure first
    .venv/Scripts/python scripts/verify_ranked.py <run_id>                # the whole slice
    .venv/Scripts/python scripts/verify_ranked.py <run_id> --restore      # undo

Nothing is deleted. A rejected pain keeps its row with `verified=0`, which `get_pains`
withholds, and `--restore` puts the slice back to `verified=1`.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from pipeline.extract import verify_run  # noqa: E402
from pipeline.store import Store  # noqa: E402

# Marker the guard respects. Without it, the first stage to write a non-terminal status has
# scripts/guard_pipeline.py relaunch the whole pipeline --from 3 on its next 10-minute tick,
# and the two writers collide on one SQLite file with "database is locked".
def _marker(run_id):
    return ROOT / "db" / ("resume-%s.maintenance" % run_id)


RANKED_SLICE = """
    SELECT p.id FROM pains p
    JOIN cluster_members cm ON cm.pain_id = p.id
    JOIN rankings r ON r.cluster_id = cm.cluster_id AND r.run_id = p.run_id
    WHERE p.run_id = ? AND r.dropped = 0 AND COALESCE(p.verified, 1) != 0
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--sample", type=int, default=0,
                    help="judge only N pains, to measure precision before paying for the rest")
    ap.add_argument("--restore", action="store_true", help="undo: put the slice back to kept")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    store = Store(cfg["db_path"])
    run = store.get_run(args.run_id)
    if not run:
        print("no such run: %s" % args.run_id)
        return 1

    if args.restore:
        n = store.conn.execute(
            "UPDATE pains SET verified=1, pain_type=NULL, verify_reason=NULL "
            "WHERE run_id=? AND verified=0 AND verify_reason IS NOT NULL AND "
            "pain_type IS NOT NULL", (args.run_id,)).rowcount
        store.conn.commit()
        print("restored %d judged pains to kept" % n)
        return 0

    ids = [r[0] for r in store.conn.execute(RANKED_SLICE, (args.run_id,))]
    if args.sample and args.sample < len(ids):
        # Spread the sample across themes. The query returns pains grouped by cluster, so
        # taking the first N would measure the precision of the top few themes only.
        step = len(ids) / args.sample
        ids = [ids[int(i * step)] for i in range(args.sample)]
    print("%s: %d pains inside ranked themes; ~%d judge calls at batch_size %d"
          % (args.run_id, len(ids), round(len(ids) / max(1, cfg["verify"]["batch_size"])),
             cfg["verify"]["batch_size"]))
    if not ids:
        return 0
    if not args.apply:
        print("  dry run - pass --apply to judge them")
        return 0

    marker = _marker(args.run_id)
    marker.write_text("verify backfill over the ranked slice", encoding="utf-8")
    stage, status = run["stage"], run["status"]
    try:
        # Every pain in this run is already verified IS NULL (insert_pain never writes the
        # column), so resetting is a no-op here -- but a slice chosen after an earlier verify
        # pass would need it, and `pain_ids` below is what actually bounds the work.
        store.conn.executemany("UPDATE pains SET verified=NULL WHERE id=?",
                               [(i,) for i in ids])
        store.conn.commit()
        t = time.time()
        done = [0]

        # classify_candidates calls progress(done, total, kept) -- three arguments, not two.
        def progress(d, total, kept=0):
            if d - done[0] >= 200 or d == total:
                done[0] = d
                print("  judged %d/%d, %d kept (%.0fs)"
                      % (d, total, kept, time.time() - t), flush=True)

        result = verify_run(store, args.run_id, cfg.get("verify", {}),
                            cfg.get("extract", {}), progress=progress, pain_ids=ids)
        print("verify: %s" % result)
        judged = result.get("judged") or 0
        kept = result.get("kept") or 0
        if judged:
            print("  measured precision on the ranked slice: %.0f%% (%d kept of %d judged)"
                  % (100 * kept / judged, kept, judged))
    finally:
        # Any pain the judge never returned is still NULL; get_pains keeps NULL, so the run
        # stays usable either way. Put the run back where it was and release the guard.
        store.set_stage(args.run_id, stage, status)
        marker.unlink(missing_ok=True)
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
