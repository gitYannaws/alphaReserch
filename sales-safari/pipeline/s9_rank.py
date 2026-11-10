"""Stage 9: rank demand against persistence and software-solvability.

rank = demand * persistence_multiplier * solvable_weight
persistence_multiplier = clamp(0.75 + 0.5 * (persistence - 1) / 9, 0.75, 1.25)
solvable_weight = weight[solvable] * (0.5 + 0.5 * confidence)

Persistence comes from demand_scores (stage 6 - it is a property of the pain, not the
competition). It is bounded because, multiplied raw (1-10), it was the strongest rank driver
on run pbfamily0001 (Spearman +0.62, ahead of demand) and a pain first seen last year could
never reach the top. solvable_weight down-weights themes a pure software product can't
address, and scales by the 7b judge's own confidence, which used to be stored and never read.

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

# "no" and "unknown" only matter when rank.allowed_solvability includes them.
DEFAULT_SOLVABLE_WEIGHTS = {"yes": 1.0, "partial": 0.6, "no": 0.25, "unknown": 0.5}
# Only the closed platform's owner can ship the fix, so no allowed_solvability admits it.
PLATFORM_FEATURE = "platform_feature"
# A real stage-7b verdict, as opposed to "unknown" (never judged) or NULL (7b never ran).
VALID_SOLVABILITY = {"yes", "partial", "no", PLATFORM_FEATURE}
FORMULA = ("demand * persistence_multiplier * solvable_weight; "
           "persistence_multiplier = clamp(0.75 + 0.5 * (persistence - 1) / 9, 0.75, 1.25); "
           "solvable_weight = weight[solvable] * (0.5 + 0.5 * confidence)")


def persistence_multiplier(persistence: float) -> float:
    return min(1.25, max(0.75, 0.75 + 0.5 * (persistence - 1) / 9))


def cluster_support(cluster: dict) -> dict:
    """Evidence / author / thread counts for one theme.

    Public because stage 7b uses the SAME rule to decide which themes are worth paying an
    LLM to classify. Two copies of this arithmetic would drift, and a 7b that skipped a
    theme rank would have accepted is a silently missing opportunity.
    """
    threads = {
        p.get("thread_url") or p.get("title") or p.get("source_permalink")
        for p in cluster.get("pains", [])
        if p.get("thread_url") or p.get("title") or p.get("source_permalink")
    }
    return {
        "evidence_count": len(cluster.get("pains", [])),
        "distinct_authors": int(cluster.get("distinct_authors") or 0),
        "distinct_threads": len(threads),
    }


def _support_by_cluster(store, run_id: str) -> dict:
    return {c["id"]: cluster_support(c) for c in store.get_cluster_details(run_id)}


def support_reasons(support: dict, thresholds: dict) -> list:
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
             progress=None, allowed_solvability: list[str] = None,
             drop_warnings: list[str] = None) -> dict:
    weights = {**DEFAULT_SOLVABLE_WEIGHTS, **(solvable_weights or {})}
    min_support = min_support or {}
    # Warning tags are advisory BY DEFAULT and that rule stands - a caveat must not quietly
    # delete a theme. `drop_warnings` is the operator naming, explicitly and in config, the
    # few tags that do disqualify. It exists for `platform_meta`: a forum corpus is full of
    # people complaining about the forum, and those are real complaints that are not this
    # market's pain. Run pbfamily0001 ranked "moderator abuse and unfair bans" at 8 and
    # "subreddit toxicity" at 11; the re-run put "frequent unwanted posts on home page" at 4.
    # The theme stays in the database with its tag, visible in the audit. It just stops
    # occupying a slot in the ranked opportunities.
    disqualifying = {str(w).strip().lower() for w in (drop_warnings or []) if str(w).strip()}
    allowed = None if allowed_solvability is None else {
        str(value).strip().lower() for value in allowed_solvability if str(value).strip()
    }
    support_map = _support_by_cluster(store, run_id) if min_support else {}
    # A theme 7b never evaluated. Not a verdict, and NOT the same as "unknown" (asked, no
    # answer): rank must not blame solvability for a theme it was never worth asking about.
    not_evaluated = "not_evaluated"
    rows = store.conn.execute(
        # Warning tags now ride on soft_filters (stage 7 folded into 7b), so there is one
        # advisory row per theme. Warnings are advisory only - they never drop a theme;
        # min_support is the sole gate here.
        "SELECT c.id,COALESCE(ds.demand_score,0),COALESCE(ds.persistence_score,3),"
        "COALESCE(ci.saturation_score,0),sf.confidence,"
        "COALESCE(sf.warnings,'[]'),sf.solvable "
        "FROM clusters c "
        "LEFT JOIN demand_scores ds ON ds.cluster_id=c.id "
        "LEFT JOIN competitive_intel ci ON ci.cluster_id=c.id "
        "LEFT JOIN soft_filters sf ON sf.cluster_id=c.id "
        "WHERE c.run_id=?",
        (run_id,)).fetchall()
    # Stage 7b is ADVISORY - the runner continues when it fails. But a missing verdict reads
    # as "unknown", and "unknown" is not in allowed_solvability, so a total 7b failure used to
    # drop EVERY theme silently: run 9af5b27db46e ranked=0 dropped=619 off one JSONDecodeError.
    # An advisory stage must be able to degrade the ranking, never empty it. When 7b produced
    # no verdict at all, rank the run without the solvability gate and say so.
    verdicts = sum(1 for r in rows if (r[6] or "").strip().lower() in VALID_SOLVABILITY)
    gate_disabled = allowed is not None and rows and not verdicts
    if gate_disabled:
        print("  WARNING: no stage-7b solvability verdicts for this run; "
              "ranking WITHOUT the solvability gate (themes are unfiltered for software fit)")
        allowed = None
    ranked = []
    for r in rows:
        demand, persistence, saturation = float(r[1]), float(r[2]), float(r[3])
        solvable = (r[6] or "unknown").strip().lower()
        # No verdict means no confidence to weigh; 0.5 scales every such theme alike.
        confidence = float(r[4]) if solvable in VALID_SOLVABILITY and r[4] is not None else 0.5
        base_weight = weights.get(solvable, weights["unknown"])
        sw = base_weight * (0.5 + 0.5 * confidence)
        multiplier = persistence_multiplier(persistence)
        # saturation is carried through for display only - see module docstring.
        base = demand * multiplier
        support = support_map.get(r[0], {})
        support_gaps = support_reasons(support, min_support)
        if solvable == PLATFORM_FEATURE:
            solvability_reasons = [PLATFORM_FEATURE]
        elif solvable == not_evaluated and support_gaps:
            # It failed support, which is WHY 7b skipped it. Report that, once.
            solvability_reasons = []
        else:
            solvability_reasons = [] if allowed is None or solvable in allowed else [
                "not_rankable_solvability"
            ]
        reasons = json.loads(r[5] or "[]")
        blocking = [w for w in reasons if str(w).strip().lower() in disqualifying]
        dropped = bool(support_gaps or solvability_reasons or blocking)
        rank_score = 0.0 if dropped else round(base * sw, 2)
        reasons.extend(support_gaps)
        reasons.extend(solvability_reasons)
        ranked.append({
            "cluster_id": r[0],
            "demand_score": demand,
            "persistence_score": persistence,
            "saturation_score": saturation,
            "dropped": dropped,
            "filter_reasons": reasons,
            "solvable_weight": sw,
            "rank_breakdown": {
                "formula": FORMULA,
                "demand": demand,
                "persistence": persistence,
                "persistence_multiplier": multiplier,
                "saturation_display_only": saturation,
                "solvable": solvable,
                "confidence": confidence,
                "solvable_base_weight": base_weight,
                "solvable_weight": sw,
                "warning_flags": reasons,
                "disqualifying_warnings": blocking,
                "support": support,
                "min_support": min_support,
                "allowed_solvability": sorted(allowed) if allowed is not None else None,
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
    return {"ranked": len([r for r in ranked if not r["dropped"]]),
            "dropped": len([r for r in ranked if r["dropped"]]),
            "dropped_by_warning": len([r for r in ranked
                                       if r["rank_breakdown"]["disqualifying_warnings"]]),
            "solvability_gate": "disabled_no_verdicts" if gate_disabled else "applied"}
