"""Score a completed analyze run against the planted synthetic truth.

    .venv/Scripts/python bench/score_synth.py <job_id>

Reports a per-stage funnel that no existing harness produces:

  stage 3  extract  - recall on planted docs, false-positive rate per distractor class
  stage 5  cluster  - was each theme gathered, and how pure is the cluster that gathered it
  stage 8  filter   - did it survive, and if not which reason fired
  stage 10 ideate   - did a SaaS-shaped theme reach an idea

The point is attribution. Run 329746779047 lost 1,803 of 1,817 clusters and nothing said
whether that was correct. Here every loss is either expected (planted below threshold) or a
miss, and the table says which.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

DB = "db/safari.sqlite"
TRUTH = Path("db/synth-truth.json")


def pct(a: int, b: int) -> str:
    return f"{a/b:6.1%}" if b else "     -"


def main(job_id: str) -> int:
    if not TRUTH.exists():
        print(f"no {TRUTH} - run bench/build_synth_corpus.py first")
        return 2
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB)

    run = conn.execute("SELECT stage,status FROM runs WHERE job_id=?", (job_id,)).fetchone()
    if not run:
        print(f"unknown job_id {job_id}")
        return 2
    print(f"run {job_id}  stage={run[0]} status={run[1]}   corpus={truth['corpus_key']}")
    print(f"built {truth['built_at'][:19]}  seed={truth['seed']}  "
          f"noise_ratio={truth['noise_ratio']}  hybrid_real={truth.get('hybrid_real_linked',0):,}\n")

    # url -> theme, for planted docs; url -> class, for distractors
    url_theme = {u: t["id"] for t in truth["themes"] for u in t["post_urls"]}
    noise_cls = {u: u.split("/")[3] for u in truth["noise_source_urls"]}
    themes = {t["id"]: t for t in truth["themes"]}

    # document_id -> source_url for everything this run touched
    docs = dict(conn.execute(
        "SELECT d.id, d.source_url FROM run_documents rd JOIN documents d ON d.id=rd.document_id "
        "WHERE rd.run_id=?", (job_id,)).fetchall())

    # ---------------------------------------------------------------- stage 3
    pain_docs = {r[0] for r in conn.execute(
        "SELECT DISTINCT document_id FROM pains WHERE run_id=? AND COALESCE(verified,1)!=0",
        (job_id,))}
    planted_hit = Counter()
    planted_tot = Counter()
    for doc_id, url in docs.items():
        tid = url_theme.get(url)
        if tid:
            planted_tot[tid] += 1
            if doc_id in pain_docs:
                planted_hit[tid] += 1
    noise_hit, noise_tot = Counter(), Counter()
    for doc_id, url in docs.items():
        cls = noise_cls.get(url)
        if cls:
            noise_tot[cls] += 1
            if doc_id in pain_docs:
                noise_hit[cls] += 1

    tot_p, tot_h = sum(planted_tot.values()), sum(planted_hit.values())
    print("STAGE 3  EXTRACT")
    print(f"  planted docs yielding a pain   {tot_h:>4}/{tot_p:<4} {pct(tot_h, tot_p)}  <- recall")
    for cls in sorted(noise_tot):
        print(f"  distractor '{cls}' flagged       {noise_hit[cls]:>4}/{noise_tot[cls]:<4} "
              f"{pct(noise_hit[cls], noise_tot[cls])}  <- false positives")
    tn, th = sum(noise_tot.values()), sum(noise_hit.values())
    prec = tot_h / (tot_h + th) if (tot_h + th) else 0
    print(f"  distractors overall            {th:>4}/{tn:<4} {pct(th, tn)}")
    print(f"  precision (planted / all pain docs)        {prec:6.1%}\n")

    # ---------------------------------------------------------------- stage 5
    # cluster -> which themes its member pains came from
    cluster_mix = defaultdict(Counter)
    for cid, doc_id in conn.execute(
            "SELECT cl.id, p.document_id FROM clusters cl "
            "JOIN cluster_members cm ON cm.cluster_id=cl.id "
            "JOIN pains p ON p.id=cm.pain_id WHERE cl.run_id=?", (job_id,)):
        url = docs.get(doc_id, "")
        cluster_mix[cid][url_theme.get(url) or f"noise:{noise_cls.get(url,'real')}"] += 1

    best = {}   # theme -> (cluster_id, n_from_theme, cluster_size)
    for cid, mix in cluster_mix.items():
        for tid, n in mix.items():
            if tid.startswith("noise:"):
                continue
            if tid not in best or n > best[tid][1]:
                best[tid] = (cid, n, sum(mix.values()))

    # ---------------------------------------------------------------- stages 8, 10
    ranked = {r[0]: r for r in conn.execute(
        "SELECT cluster_id, rank, rank_score, dropped, filter_reasons FROM rankings WHERE run_id=?",
        (job_id,))}
    idea_clusters = {r[0] for r in conn.execute(
        "SELECT cluster_id FROM ideas WHERE run_id=?", (job_id,))}

    print("PER-THEME OUTCOME")
    print(f"  {'id':<4} {'aut':>3} {'thr':>3} {'ext':>7} {'clustered':>10} {'purity':>7} "
          f"{'survived':>9} {'idea':>5}  {'expected':>8}  verdict")
    print("  " + "-" * 104)
    correct = 0
    for tid, t in themes.items():
        hit, tot = planted_hit[tid], planted_tot[tid]
        cid, n_from, size = best.get(tid, (None, 0, 0))
        purity = n_from / size if size else 0
        row = ranked.get(cid) if cid else None
        survived = bool(row and not row[3])
        got_idea = bool(cid and cid in idea_clusters)
        ok = survived == t["expect_survives"]
        correct += ok
        reasons = ""
        if row and row[3]:
            try:
                reasons = ",".join(json.loads(row[4] or "[]"))[:34]
            except Exception:
                reasons = str(row[4])[:34]
        print(f"  {tid:<4} {t['n_authors']:>3} {t['n_threads']:>3} {hit:>3}/{tot:<3} "
              f"{('yes' if cid else 'NO'):>10} {purity:>6.0%} "
              f"{('yes' if survived else 'no'):>9} {('yes' if got_idea else '-'):>5}  "
              f"{('survive' if t['expect_survives'] else 'drop'):>8}  "
              f"{'OK' if ok else 'MISMATCH'} {reasons}")

    saas = [t for t in themes.values() if t["saas_shaped"] and t["expect_survives"]]
    saas_ideas = sum(1 for t in saas if best.get(t["id"], (None,))[0] in idea_clusters)
    print("\nSUMMARY")
    print(f"  themes planted                 {len(themes)}")
    print(f"  survive/drop matched spec      {correct}/{len(themes)}  {pct(correct, len(themes))}")
    print(f"  SaaS-shaped themes -> an idea  {saas_ideas}/{len(saas)}  {pct(saas_ideas, len(saas))}"
          "   <- end-to-end yield")
    print(f"  extraction recall              {pct(tot_h, tot_p)}")
    print(f"  extraction precision           {prec:6.1%}")
    if truth.get("hybrid_real_linked"):
        print("\n  hybrid mode: real forum text was mixed in, so these are the pessimistic")
        print("  bounds. Compare against a clean-room build to size the realism gap.")
    else:
        print("\n  clean-room build: synthetic prose is tidier than forum prose, so extraction")
        print("  recall here is an UPPER bound. Re-run with --hybrid for the other end.")
    conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_id")
    sys.exit(main(ap.parse_args().job_id))
