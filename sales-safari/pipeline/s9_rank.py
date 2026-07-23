"""Stage 9: rank demand against persistence and software-solvability.

rank = demand * persistence * solvable_weight

Persistence comes from demand_scores (stage 6 - it is a property of the pain, not the
competition). solvable_weight down-weights themes a pure software product can't address,
so social/hardware pains don't outrank software-shaped ones.

SATURATION IS NOT IN THIS FORMULA (changed 2026-07-20). It used to divide as
`/(1 + saturation)`, set by a competitor stage that ran just before. That was measurably
backwards: on run 9af5b27db46e discovery found competitors for only 8 of 642 themes, so
those 8 took a 5x penalty while the 615 the model knew nothing about ranked as if the field
were empty - the formula rewarded ignorance, and the 8 understood themes landed at ranks
151 and 209-213 of 213. Competitor discovery now runs AFTER rank against the drafted ideas
(stage 9b), where its output is evidence for the brief rather than a penalty on the score.
Saturation is still recorded on competitive_intel for display.
"""
import json

DEFAULT_SOLVABLE_WEIGHTS = {"yes": 1.0, "partial": 0.6, "no": 0.25, "unknown": 0.5}


def _support_by_cluster(store, run_id: str) -> dict:
    support = {}
    for cluster in store.get_cluster_details(run_id):
        threads = {
            p.get("thread_url") or p.get("title") or p.get("source_permalink")
            for p in cluster.get("pains", [])
            if p.get("thread_url") or p.get("title") or p.get("source_permalink")
        }
        support[cluster["id"]] = {
            "evidence_count": len(cluster.get("pains", [])),
            "distinct_authors": int(cluster.get("distinct_authors") or 0),
            "distinct_threads": len(threads),
        }
    return support


def _support_reasons(support: dict, thresholds: dict) -> list:
    reasons = []
    checks = (
        ("evidence_count", "insufficient_evidence"),
        ("distinct_authors", "insufficient_authors"),
        ("distinct_threads", "insufficient_threads"),
    )
    for key, reason in checks:
        need = int(thresholds.get(key) or 0)
        if need and int(support.get(key) or 0) < need:
            reasons.append(reason)
    return reasons


def rank_run(store, run_id: str, solvable_weights: dict = None, min_support: dict = None,
             progress=None) -> dict:
    weights = {**DEFAULT_SOLVABLE_WEIGHTS, **(solvable_weights or {})}
    min_support = min_support or {}
    support_map = _support_by_cluster(store, run_id) if min_support else {}
    rows = store.conn.execute(
        # Warning tags now ride on soft_filters (stage 7 folded into 7b), so there is one
        # advisory row per theme. Warnings are advisory only - they never drop a theme;
        # min_support is the sole gate here.
        "SELECT c.id,COALESCE(ds.demand_score,0),COALESCE(ds.persistence_score,3),"
        "COALESCE(ci.saturation_score,0),0,"
        "COALESCE(sf.warnings,'[]'),sf.solvable "
        "FROM clusters c "
        "LEFT JOIN demand_scores ds ON ds.cluster_id=c.id "
        "LEFT JOIN competitive_intel ci ON ci.cluster_id=c.id "
        "LEFT JOIN soft_filters sf ON sf.cluster_id=c.id "
        "WHERE c.run_id=?",
        (run_id,)).fetchall()
    ranked = []
    for r in rows:
        demand, persistence, saturation = float(r[1]), float(r[2]), float(r[3])
        solvable = (r[6] or "unknown").strip().lower()
        sw = weights.get(solvable, weights["unknown"])
        # saturation is carried through for display only - see module docstring.
        base = demand * persistence
        support = support_map.get(r[0], {})
        support_reasons = _support_reasons(support, min_support)
        dropped = bool(r[4] or support_reasons)
        rank_score = 0.0 if dropped else round(base * sw, 2)
        reasons = json.loads(r[5] or "[]")
        reasons.extend(support_reasons)
        ranked.append({
            "cluster_id": r[0],
            "demand_score": demand,
            "persistence_score": persistence,
            "saturation_score": saturation,
            "dropped": dropped,
            "filter_reasons": reasons,
            "solvable_weight": sw,
            "rank_breakdown": {
                "formula": "demand * persistence * solvable_weight",
                "demand": demand,
                "persistence": persistence,
                "saturation_display_only": saturation,
                "solvable": solvable,
                "solvable_weight": sw,
                "warning_flags": reasons,
                "support": support,
                "min_support": min_support,
            },
            "rank_score": rank_score,
        })
    ranked.sort(key=lambda x: (x["dropped"], -x["rank_score"]))
    store.clear_rankings(run_id)
    if progress:
        progress(0, len(ranked))
    rank = 1
    for i, row in enumerate(ranked, start=1):
        row["rank"] = None if row["dropped"] else rank
        if not row["dropped"]:
            rank += 1
        store.save_ranking(run_id, row)
        if progress:
            progress(i, len(ranked))
    store.set_stage(run_id, 9, "ranked")
    return {"ranked": len([r for r in ranked if not r["dropped"]]), "dropped": len([r for r in ranked if r["dropped"]])}
