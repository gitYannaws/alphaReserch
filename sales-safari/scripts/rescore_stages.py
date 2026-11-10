"""Re-run stages 5-9 on a finished run, with the stage-3b judge in the middle, and leave the
run's status where it was.

Why a script and not `pipeline.resume --from 5`: the resume runner continues into the idea
chain (10a, 9b, 9c, 10b, 12), which is LLM-heavy and not what a scoring re-run is for. And
the judge has to sit BETWEEN two clusterings: the first, audit off, only exists to say which
pains can reach a rankable theme; the judge then withholds the noise among them; the second,
audit on, names the themes the survivors form. Judging before any clustering means ~7,300
calls over the whole run; judging the rankable slice is a tenth of that.

    .venv-linux/bin/python scripts/rescore_stages.py <run_id>              # all steps
    .venv-linux/bin/python scripts/rescore_stages.py <run_id> --from verify  # resume

Steps, in order: cluster1, verify, cluster2, softfilter, demand, rank. cluster2 uses the rank
gate's evidence floor (`--final-min-cluster-size`, default rank.min_support.evidence_count) as
its min_cluster_size, because the judge withholds most of a pass-1 theme. Every step is
idempotent: cluster_run clears its own rows, the judge skips pains that already carry a
verdict, softfilter/demand/rank clear and rewrite. Status and stage are restored at the end
(a maintenance run must leave the run TERMINAL, see CLAUDE.md).
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import yaml  # noqa: E402

from pipeline.cluster import cluster_run  # noqa: E402
from pipeline.extract import verify_run  # noqa: E402
from pipeline.s6_demand import demand_run  # noqa: E402
from pipeline.s7b_softfilter import softfilter_run  # noqa: E402
from pipeline.s9_rank import rank_run  # noqa: E402
from pipeline.store import Store  # noqa: E402
from verify_ranked import rankable_slice  # noqa: E402

STEPS = ("cluster1", "verify", "cluster2", "softfilter", "demand", "rank")


def _log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _cluster(store, run_id, cfg, audit: bool, min_cluster_size: int = None):
    cc = cfg.get("cluster", {})
    return cluster_run(store, run_id,
                       min_cluster_size=min_cluster_size or cc.get("min_cluster_size", 2),
                       min_cohesion=cc.get("min_cohesion", 0.55),
                       cluster_selection_method=cc.get("cluster_selection_method", "leaf"),
                       min_samples=cc.get("min_samples"),
                       cluster_selection_epsilon=cc.get("cluster_selection_epsilon", 0.0),
                       semantic_refine=audit and cc.get("semantic_refine", True),
                       semantic_label_only=cc.get("semantic_label_only", False),
                       audit_min_cluster_size=cc.get("audit_min_cluster_size", 5),
                       extract_cfg=cfg.get("extract", {}),
                       progress=lambda d, t: _log("  cluster %d/%d" % (d, t)))


def _verify(store, run_id, cfg):
    ids = rankable_slice(store, run_id, cfg.get("rank", {}).get("min_support"))
    pending = store.conn.execute(
        "SELECT count(*) FROM pains WHERE run_id=? AND verified IS NULL", (run_id,)).fetchone()[0]
    _log("verify: %d pains in rankable themes (%d in the run still unjudged)" % (len(ids), pending))
    total = {"candidates": 0, "judged": 0, "kept": 0, "rejected": 0, "failed_batches": 0,
             "by_type": {}}
    t0 = time.time()
    last = [0]

    def progress(d, n, kept=0):
        if d - last[0] >= 200 or d == n:
            last[0] = d
            _log("  judged %d/%d in this chunk, %d kept (%.0fs)" % (d, n, kept, time.time() - t0))

    for i in range(0, len(ids), 5000):
        part = verify_run(store, run_id, cfg.get("verify", {}), cfg.get("extract", {}),
                          progress=progress, pain_ids=ids[i:i + 5000])
        for k in ("candidates", "judged", "kept", "rejected", "failed_batches"):
            total[k] += part.get(k) or 0
        for k, v in (part.get("by_type") or {}).items():
            total["by_type"][k] = total["by_type"].get(k, 0) + v
    return total


def _softfilter(store, run_id, cfg):
    sf_cfg = cfg.get("soft_filter", {})
    return softfilter_run(store, run_id, cfg.get("extract", {}),
                          progress=lambda d, t: None,
                          batch_size=sf_cfg.get("batch_size", 40),
                          max_batch_chars=sf_cfg.get("max_batch_chars", 8000),
                          enabled_filters=cfg.get("hard_filters", []),
                          min_support=(cfg.get("rank", {}).get("min_support")
                                       if sf_cfg.get("only_rankable", True) else None))


def _demand(store, run_id, cfg):
    return demand_run(store, run_id, cfg.get("scoring_weights", {}),
                      full_at=cfg.get("score_full_at"))


def _rank(store, run_id, cfg):
    rank_cfg = cfg.get("rank", {})
    return rank_run(store, run_id,
                    solvable_weights=rank_cfg.get("solvable_weights"),
                    allowed_solvability=rank_cfg.get("allowed_solvability"),
                    min_support=rank_cfg.get("min_support"),
                    drop_warnings=rank_cfg.get("drop_warnings"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--from", dest="start", choices=STEPS, default=STEPS[0])
    ap.add_argument("--to", dest="stop", choices=STEPS, default=STEPS[-1])
    ap.add_argument("--final-min-cluster-size", type=int, default=None,
                    help="min_cluster_size for the post-judge clustering "
                         "(default: rank.min_support.evidence_count)")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if args.final_min_cluster_size is None:
        args.final_min_cluster_size = int(
            (cfg.get("rank", {}).get("min_support") or {}).get("evidence_count") or 0) or None
    store = Store(cfg["db_path"])
    run = store.get_run(args.run_id)
    if not run:
        print("no such run: %s" % args.run_id)
        return 1
    stage, status = run["stage"], run["status"]
    marker = ROOT / "db" / ("resume-%s.maintenance" % args.run_id)
    marker.write_text("rescore_stages over stages 5-9", encoding="utf-8")
    steps = STEPS[STEPS.index(args.start):STEPS.index(args.stop) + 1]
    try:
        for step in steps:
            t0 = time.time()
            _log("step %s start" % step)
            if step == "cluster1":
                r = _cluster(store, args.run_id, cfg, audit=False)
            elif step == "verify":
                r = _verify(store, args.run_id, cfg)
            elif step == "cluster2":
                # The judge withholds most members of a pass-1 theme (measured 8% kept on the
                # ranked slice), so the survivors re-form themes at the rank gate's own evidence
                # floor rather than the pass-1 size that decided who was worth judging.
                r = _cluster(store, args.run_id, cfg, audit=True,
                             min_cluster_size=args.final_min_cluster_size)
            elif step == "softfilter":
                r = _softfilter(store, args.run_id, cfg)
            elif step == "demand":
                r = _demand(store, args.run_id, cfg)
            else:
                r = _rank(store, args.run_id, cfg)
            _log("step %s done in %.0fs :: %s" % (step, time.time() - t0, r))
    finally:
        store.set_stage(args.run_id, stage, status)
        marker.unlink(missing_ok=True)
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
