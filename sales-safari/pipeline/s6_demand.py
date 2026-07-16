"""Stage 6: score demand without considering competition."""
from collections import Counter
import math
import re

INTENSITY_TERMS = {
    "can't", "cannot", "impossible", "frustrating", "frustrated", "hate",
    "broken", "stuck", "waste", "wasting", "manual", "tedious", "pain",
    "annoying", "problem", "issue", "hard", "difficult", "slow",
}
WTP_TERMS = {
    "pay", "paid", "paying", "price", "pricing", "cost", "costs", "expensive",
    "subscription", "fee", "fees", "buy", "bought", "purchased", "purchase",
    "budget", "invoice", "refund",
}
REACHABILITY_TERMS = {
    "forum", "community", "makerspace", "shop", "etsy", "client",
    "customer", "seller", "business", "hobbyist", "beginner", "class",
}


def _texts(cluster: dict):
    for p in cluster["pains"]:
        yield " ".join(str(p.get(k) or "") for k in (
            "complaint", "workflow_pain", "workaround", "wish", "persona",
            "verbatim_span"))


def _term_hits(text: str, terms: set) -> list:
    low = text.lower()
    hits = []
    for term in terms:
        pattern = r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, low):
            hits.append(term)
    return sorted(hits)


def _term_score(text: str, terms: set, base: float = 2.0) -> tuple[float, list]:
    hits = _term_hits(text, terms)
    return min(10.0, base + len(hits) * 1.5), hits


def _endorsement(cluster: dict, cap: float = 3.0) -> tuple[float, dict]:
    """Silent-agreement signal: net upvotes/likes across the cluster's pains.

    One complaint that 100 people upvoted is a market signal the distinct-author count
    misses. Log-squashed and capped so a single viral post can't dominate, and negatives
    (downvoted = disagreed, not pain) clamp to 0. Sources with no vote signal (score None)
    contribute nothing, keeping the term source-agnostic.
    """
    scored = [int(p["score"]) for p in cluster["pains"] if p.get("score") is not None]
    total_votes = sum(v for v in scored if v > 0)
    boost = round(min(cap, math.log10(1 + total_votes)) if total_votes else 0.0, 2)
    return boost, {
        "total_upvotes": total_votes,
        "posts_with_votes": len(scored),
        "top_post_votes": max(scored) if scored else 0,
        "boost": boost,
    }


def _weighted_score(parts: dict, weights: dict) -> float:
    total_weight = sum(float(weights.get(k, 1)) for k in parts) or 1.0
    return round(sum(parts[k] * float(weights.get(k, 1)) for k in parts) / total_weight, 2)


def _quotes_for_terms(cluster: dict, terms: list, limit: int = 3) -> list:
    out = []
    for p in cluster["pains"]:
        quote = str(p.get("verbatim_span") or "").strip()
        if not quote:
            continue
        low = " ".join(str(p.get(k) or "") for k in (
            "complaint", "workflow_pain", "workaround", "wish", "persona",
            "verbatim_span")).lower()
        if not terms or any(t in low for t in terms):
            out.append({
                "quote": quote[:220],
                "source": p.get("source_permalink") or "",
                "persona": p.get("persona") or "",
            })
        if len(out) >= limit:
            break
    return out


def _token_key(text: str) -> tuple:
    words = re.findall(r"[a-z0-9']{4,}", (text or "").lower())
    stop = {"with", "that", "this", "from", "have", "they", "when", "what", "would"}
    return tuple(w for w in words if w not in stop)[:8]


def _recurrence(cluster: dict) -> tuple[float, dict]:
    pains = cluster["pains"]
    distinct_authors = int(cluster.get("distinct_authors") or 0)
    evidence_count = len(pains)
    threads = {
        p.get("thread_url") or p.get("title") or p.get("source_permalink")
        for p in pains
        if p.get("thread_url") or p.get("title") or p.get("source_permalink")
    }
    workaround_count = sum(1 for p in pains if (p.get("workaround") or "").strip())
    repeated_phrases = [
        key for key, count in Counter(_token_key(p.get("workflow_pain") or p.get("complaint") or "")
                                      for p in pains).items()
        if key and count > 1
    ]
    score = min(10.0,
                distinct_authors * 1.8
                + min(2.0, len(threads) * 0.7)
                + min(2.0, workaround_count * 0.6)
                + min(2.0, len(repeated_phrases) * 0.8)
                + max(0, evidence_count - distinct_authors) * 0.3)
    return round(score, 2), {
        "distinct_authors": distinct_authors,
        "evidence_count": evidence_count,
        "distinct_threads": len(threads),
        "workaround_count": workaround_count,
        "repeated_patterns": [" ".join(k) for k in repeated_phrases[:3]],
        "quotes": _quotes_for_terms(cluster, [], limit=3),
    }


def score_cluster(cluster: dict, weights: dict) -> dict:
    text = "\n".join(_texts(cluster))
    distinct_authors = int(cluster.get("distinct_authors") or 0)
    evidence_count = len(cluster["pains"])
    post_level = sum(1 for p in cluster["pains"] if p.get("source_granularity") == "post")

    pain_intensity, intensity_hits = _term_score(text, INTENSITY_TERMS, base=3.0)
    endorsement, endorsement_evidence = _endorsement(cluster)
    frequency = min(10.0, distinct_authors * 2.5
                    + max(0, evidence_count - distinct_authors) + endorsement)
    willingness_to_pay, wtp_hits = _term_score(text, WTP_TERMS, base=1.0)
    reachability, reach_hits = _term_score(text, REACHABILITY_TERMS, base=2.0)
    if evidence_count:
        reachability = min(10.0, reachability + (post_level / evidence_count) * 2.0)
    recurrence_score, recurrence_evidence = _recurrence(cluster)

    parts = {
        "pain_intensity": round(pain_intensity, 2),
        "frequency": round(frequency, 2),
        "willingness_to_pay": round(willingness_to_pay, 2),
        "reachability": round(reachability, 2),
        "recurrence_score": recurrence_score,
    }
    return {
        **parts,
        "demand_score": _weighted_score(parts, weights),
        "evidence_count": evidence_count,
        "distinct_authors": distinct_authors,
        "scoring_evidence": {
            "pain_intensity": {
                "signals": intensity_hits,
                "quotes": _quotes_for_terms(cluster, intensity_hits),
            },
            "frequency": {
                "distinct_authors": distinct_authors,
                "evidence_count": evidence_count,
                "post_level_evidence": post_level,
                "endorsement": endorsement_evidence,
            },
            "willingness_to_pay": {
                "signals": wtp_hits,
                "quotes": _quotes_for_terms(cluster, wtp_hits),
            },
            "reachability": {
                "signals": reach_hits,
                "post_level_evidence": post_level,
            },
            "recurrence": recurrence_evidence,
        },
    }


def demand_run(store, run_id: str, weights: dict, progress=None) -> dict:
    clusters = store.get_cluster_details(run_id)
    store.clear_demand(run_id)
    if progress:
        progress(0, len(clusters))
    for i, cluster in enumerate(clusters, start=1):
        store.save_demand_score(run_id, cluster["id"], score_cluster(cluster, weights))
        if progress:
            progress(i, len(clusters))
    store.set_stage(run_id, 6, "demand_scored")
    return {"scored": len(clusters)}
