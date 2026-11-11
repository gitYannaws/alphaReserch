"""Re-run stages 5-9 on a finished run, with the stage-3b judge in the middle, and leave the
run in a terminal status however the re-score ends.

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
verdict, softfilter/demand/rank clear and rewrite.

Pass 2 clusters ONLY pass 1's rankable slice. cluster1 writes it to
`db/rescore-<run>.slice.json`; verify judges it and cluster2 clusters it minus what the judge
rejected. Taking every unrejected pain instead put 11,132 pains (41%) the judge was never
shown into pbfamily0001's pass-2 themes. A slice pain the judge did not rule on (a failed
batch, a pain it skipped) is still clustered, and cluster2 logs how many. Resuming at verify
or cluster2 needs that file, because the live clusters cannot be told apart from pass 2's;
without it, re-run from cluster1.

Every clustering reuses the ids `<run>-<n>`, so after each cluster step the run's rankings,
demand scores, soft filters, filter results, competition, competitors and their reviews,
ideas and briefs are deleted: left in place, they resolve to unrelated new themes (7,371 of
8,816 rankings and 12 of 21 ideas did on pbfamily0001). The idea chain re-runs afterwards.

The run must be finished (done, reported or cancelled) to start, and holds a status the guard
ignores for the whole re-score, because scripts/guard_pipeline.py relaunches the pipeline for
any other status (CLAUDE.md). Before each step the run is parked `cancelled`, with an error
naming that step as the resume point, so a crash anywhere, SIGKILL included, leaves a
terminal status and says where to pick up; `done` would hide that stages 5-9 hold a mix of
old and new rows. SIGTERM and SIGHUP become SystemExit, so a kill takes the failure path:
NOTIFY, the resume point, a non-zero exit. The starting status comes back only when rank
finishes and every step an earlier attempt left unfinished has run since; `--to` before rank,
or a start past an unfinished step, leaves the run parked at the step still owed. The
maintenance marker carries this process's pid and the same resume point. A marker whose pid
is gone is replaced and its resume point honoured; any other marker refuses the start. When
no terminal status can be written at all (database locked), the marker stays.
"""
import argparse
import json
import os
import re
import signal
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
TERMINAL = {"done", "reported", "cancelled"}
# The status a re-score overwrote and the step to resume from, kept at the end of the run's
# error and of the marker so a later start can give that status back. The marker copy also
# survives a SIGKILL and a status write that failed.
RESTORE = re.compile(r"\[restore: (\d+) (\S+) (\S+)\]$")


def _log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _slice_path(run_id):
    return ROOT / "db" / ("rescore-%s.slice.json" % run_id)


def _write_slice(run_id, pain_ids):
    path = _slice_path(run_id)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"run_id": run_id, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "pain_ids": pain_ids}), encoding="utf-8")
    os.replace(tmp, path)
    _log("pass-1 rankable slice: %d pains -> %s" % (len(pain_ids), path))


def _read_slice(run_id):
    data = json.loads(_slice_path(run_id).read_text(encoding="utf-8"))
    _log("pass-1 rankable slice from %s: %d pains" % (data["created_at"], len(data["pain_ids"])))
    return data["pain_ids"]


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return True  # os.kill(pid, 0) TERMINATES the process on Windows; unknown counts as alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _park(store, marker, run_id, stage, status, step, note):
    """Leave the run `cancelled`, which the guard ignores, with the step to resume from."""
    tag = "[restore: %s %s %s]" % (stage, status, step)
    marker.write_text("rescore_stages pid=%d %s" % (os.getpid(), tag), encoding="utf-8")
    msg = ("%s: stages 5-9 hold a MIX of old and new rows. Resume with: "
           "scripts/rescore_stages.py %s --from %s %s" % (note, run_id, step, tag))
    store.cancel_run(run_id, stage, msg)
    return msg


def _raise_exit(signum, frame):
    raise SystemExit("killed by %s" % signal.Signals(signum).name)


def _cluster(store, run_id, cfg, audit: bool, min_cluster_size: int = None,
             only_pain_ids=None):
    cc = cfg.get("cluster", {})
    r = cluster_run(store, run_id,
                    min_cluster_size=min_cluster_size or cc.get("min_cluster_size", 2),
                    min_cohesion=cc.get("min_cohesion", 0.55),
                    cluster_selection_method=cc.get("cluster_selection_method", "leaf"),
                    min_samples=cc.get("min_samples"),
                    cluster_selection_epsilon=cc.get("cluster_selection_epsilon", 0.0),
                    semantic_refine=audit and cc.get("semantic_refine", True),
                    semantic_label_only=cc.get("semantic_label_only", False),
                    audit_min_cluster_size=cc.get("audit_min_cluster_size", 5),
                    extract_cfg=cfg.get("extract", {}),
                    progress=lambda d, t: _log("  cluster %d/%d" % (d, t)),
                    only_pain_ids=only_pain_ids)
    baseline = sorted((ROOT / "db" / "baseline").glob(run_id + "*"))
    _log("cleared stale rows: %s (%s)" % (
        store.clear_cluster_dependents(run_id),
        "baseline copy: db/baseline/%s" % baseline[0].name if baseline
        else "no baseline copy in db/baseline/"))
    return r


def _judged_slice(store, run_id):
    """The pass-1 slice, saying out loud how much of it carries no judge verdict."""
    ids = _read_slice(run_id)
    open_ids = dict(store.conn.execute(
        "SELECT id, verified IS NULL FROM pains WHERE run_id=? "
        "AND (verified IS NULL OR pain_type='unjudged')", (run_id,)))
    never = sum(1 for i in ids if open_ids.get(i) == 1)
    omitted = sum(1 for i in ids if open_ids.get(i) == 0)
    if never or omitted:
        _log("WARN cluster2: of %d slice pains, %d were never judged (--from verify judges them) "
             "and %d came back from the judge without a verdict; both are clustered"
             % (len(ids), never, omitted))
    return ids


def _verify(store, run_id, cfg, ids):
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
    r = softfilter_run(store, run_id, cfg.get("extract", {}),
                       progress=lambda d, t: None,
                       batch_size=sf_cfg.get("batch_size", 40),
                       max_batch_chars=sf_cfg.get("max_batch_chars", 8000),
                       enabled_filters=cfg.get("hard_filters", []),
                       min_support=(cfg.get("rank", {}).get("min_support")
                                    if sf_cfg.get("only_rankable", True) else None))
    if r.get("evidence_missing"):
        _log("WARN softfilter: %d of %d verdicts came without evidence fields (stored NULL, "
             "yes downgraded to partial)" % (r["evidence_missing"], r["classified"]))
    return r


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
    ap.add_argument("--no-audit", action="store_true",
                    help="skip the LLM naming audit in cluster2 (themes get a central-member "
                         "quote as their name; ~12 s per theme saved)")
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
    stage, status, resume_at = run["stage"], run["status"], None
    marker = ROOT / "db" / ("resume-%s.maintenance" % args.run_id)
    held = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    owner = re.search(r"\bpid=(\d+)", held or "")
    prior = (RESTORE.search(held or "")
             or (status == "cancelled" and RESTORE.search(run["error"] or "")))
    if prior:
        stage, status, resume_at = int(prior.group(1)), prior.group(2), prior.group(3)
    steps = STEPS[STEPS.index(args.start):STEPS.index(args.stop) + 1]
    if held is not None and (not owner or _pid_alive(int(owner.group(1)))):
        refusal = ("%s is held (%r) and its owner may still be running. If no rescore_stages "
                   "or verify_ranked process works on %s, delete the file and start again."
                   % (marker, held, args.run_id))
    elif status not in TERMINAL:
        refusal = ("run %s is at stage %s status %r, which is unfinished: a runner may own it. "
                   "Re-score a run whose status is one of %s."
                   % (args.run_id, stage, status, sorted(TERMINAL)))
    elif ({"verify", "cluster2"} & set(steps) and "cluster1" not in steps
          and not _slice_path(args.run_id).exists()):
        refusal = ("%s is missing. verify and cluster2 work on pass 1's rankable slice, and the "
                   "live clusters cannot be told apart from pass 2's. Re-run from cluster1: "
                   "scripts/rescore_stages.py %s --from cluster1"
                   % (_slice_path(args.run_id), args.run_id))
    else:
        refusal = None
    if refusal:
        print("refusing: " + refusal)
        store.close()
        return 1
    if held is not None:
        _log("%s: pid %s is gone%s; replacing the marker"
             % (marker.name, owner.group(1),
                " (it stopped in step %s)" % resume_at if resume_at else ""))
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _raise_exit)
    step, settled = steps[0], False
    try:
        for step in steps:
            t0 = time.time()
            _park(store, marker, args.run_id, stage, status, step,
                  "rescore_stages pid %d is in step %s, or died there" % (os.getpid(), step))
            _log("step %s start" % step)
            if step == "cluster1":
                r = _cluster(store, args.run_id, cfg, audit=False)
                _write_slice(args.run_id, rankable_slice(
                    store, args.run_id, cfg.get("rank", {}).get("min_support")))
            elif step == "verify":
                r = _verify(store, args.run_id, cfg, _read_slice(args.run_id))
            elif step == "cluster2":
                # The judge withholds most members of a pass-1 theme (measured 8% kept on the
                # ranked slice), so the survivors re-form themes at the rank gate's own evidence
                # floor rather than the pass-1 size that decided who was worth judging.
                r = _cluster(store, args.run_id, cfg, audit=not args.no_audit,
                             min_cluster_size=args.final_min_cluster_size,
                             only_pain_ids=_judged_slice(store, args.run_id))
            elif step == "softfilter":
                r = _softfilter(store, args.run_id, cfg)
            elif step == "demand":
                r = _demand(store, args.run_id, cfg)
            else:
                r = _rank(store, args.run_id, cfg)
            _log("step %s done in %.0fs :: %s" % (step, time.time() - t0, r))
        if resume_at and STEPS.index(resume_at) < STEPS.index(steps[0]):
            left = (resume_at, "rescore_stages skipped step %s, which never finished" % resume_at)
        elif steps[-1] != STEPS[-1]:
            left = (STEPS[STEPS.index(steps[-1]) + 1],
                    "rescore_stages stopped after step %s (--to)" % steps[-1])
        else:
            left = None
        if left:
            _log(_park(store, marker, args.run_id, stage, status, *left))
        else:
            store.set_stage(args.run_id, stage, status)
        settled = True
    except BaseException as e:
        # A signal can land between a step's write and its commit: drop that half-write
        # rather than commit it along with the status.
        store.conn.rollback()
        _log("NOTIFY " + _park(store, marker, args.run_id, stage, status, step,
                               "rescore_stages stopped in step %s (%s)" % (step, repr(e)[:200])))
        settled = True
        raise
    finally:
        if settled:
            marker.unlink(missing_ok=True)
        else:
            _log("NOTIFY no terminal status could be written for %s; %s stays so the guard keeps "
                 "off, and the next start resumes from it" % (args.run_id, marker))
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
