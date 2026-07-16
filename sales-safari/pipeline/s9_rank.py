"""Stage 9: rank demand against persistence, saturation, and software-solvability.

rank = demand * persistence / (1 + saturation) * solvable_weight

Saturation comes from real competitor counts (backfilled after competitor discovery, which
the caller runs before re-ranking). solvable_weight down-weights themes a pure software
product can't address, so social/hardware pains don't outrank software-shaped ones.
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
        "SELECT c.id,COALESCE(ds.demand_score,0),COALESCE(ci.persistence_score,3),"
        "COALESCE(ci.saturation_score,0),COALESCE(fr.dropped,0),"
        "COALESCE(fr.reasons,'[]'),sf.solvable "
        "FROM clusters c "
        "LEFT JOIN demand_scores ds ON ds.cluster_id=c.id "
        "LEFT JOIN competitive_intel ci ON ci.cluster_id=c.id "
        "LEFT JOIN filter_results fr ON fr.cluster_id=c.id "
        "LEFT JOIN soft_filters sf ON sf.cluster_id=c.id "
        "WHERE c.run_id=?",
        (run_id,)).fetchall()
    ranked = []
    for r in rows:
        demand, persistence, saturation = float(r[1]), float(r[2]), float(r[3])
        solvable = (r[6] or "unknown").strip().lower()
        sw = weights.get(solvable, weights["unknown"])
        base = (demand * persistence) / (1.0 + saturation)
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
                "formula": "demand * persistence / (1 + saturation) * solvable_weight",
                "demand": demand,
                "persistence": persistence,
                "saturation": saturation,
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
