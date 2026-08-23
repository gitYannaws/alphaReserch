"""Stage 6: score demand without considering competition."""
from collections import Counter
from datetime import datetime, timezone
import math
import re

# How many voices / threads / months earn a full 10 on a log curve. These are the knobs that
# decide whether SIZE is visible in the score at all.
#
# The old maths was linear with a hard `min(10, ...)`: frequency was `authors * 2.5`, which
# hits the ceiling at FOUR authors, and recurrence was `authors * 1.8`, which hits it at six.
# Measured on run pbfamily0001, that pinned frequency at 10.00 for 40% of all themes and for
# effectively every theme that ranked -- so a 5-author theme and a 23-author theme scored
# identically, and the top of the ranking filled with the smallest themes that could clear
# min_support. The upvote signal was computed and then thrown away by the same ceiling,
# despite 177,130 documents carrying a score of up to 2,990.
#
# A log curve keeps rising instead. Set the anchor near the biggest theme a healthy run
# produces, so real differences spread across the range rather than piling up at the cap.
DEFAULT_FULL_SCORE_AT = {"voices": 25, "threads": 12, "months": 36}

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
# Persistence: does the pain keep recurring despite attempts? A pain signal about the pain
# itself (not competitors), so it lives here in demand, not in the competition stage. Still a
# keyword proxy for the real test (complaint dates vs incumbent launch) - see CLAUDE.md 8.3.
PERSISTENCE_TERMS = {
    "still", "again", "keeps", "keep having", "after update", "workaround",
    "manual", "every time", "always", "still can't", "still cannot",
}


def _log_scale(value: float, full_at: float, cap: float = 10.0) -> float:
    """Map 0..inf onto 0..cap on a log curve that reaches `cap` at `full_at`.

    Monotonic all the way up: doubling the evidence always raises the score, it just raises
    it less each time. That is the property a linear-then-clamp score does not have.
    """
    if value <= 0 or full_at <= 0:
        return 0.0
    return min(cap, cap * math.log10(1.0 + value) / math.log10(1.0 + full_at))


def _parse_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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


def _recurrence(cluster: dict, full_at: dict) -> tuple[float, dict]:
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
    # Recurrence used to lead with `distinct_authors * 1.8`, which made it a second, noisier
    # copy of frequency -- both pinned at 10.00 on every ranked theme of run pbfamily0001.
    # Recurrence is about the pain COMING BACK: the same complaint raised in separate
    # threads, people describing workarounds, the same phrasing recurring. Author count is
    # frequency's job and is not counted a second time here.
    score = min(10.0,
                _log_scale(len(threads), full_at.get("threads", 12), cap=6.0)
                + min(2.0, workaround_count * 0.6)
                + min(2.0, len(repeated_phrases) * 0.8))
    return round(score, 2), {
        "distinct_authors": distinct_authors,
        "evidence_count": evidence_count,
        "distinct_threads": len(threads),
        "workaround_count": workaround_count,
        "repeated_patterns": [" ".join(k) for k in repeated_phrases[:3]],
        "quotes": _quotes_for_terms(cluster, [], limit=3),
    }


def _persistence(cluster: dict, text: str, corpus_latest, full_at: dict) -> tuple[float, dict]:
    """Does this pain KEEP COMING BACK, and is it still live?

    CLAUDE.md called the old version "a keyword proxy, not yet the true
    complaint-date-vs-launch test", and it showed. Counting words like "still" and "again"
    across the concatenated cluster text gave 7,775 of run pbfamily0001's 8,925 themes the
    exact same 3.0, and only 4 distinct values in the entire run. For a rank MULTIPLIER that
    is the same as not existing. Worse, a longer theme contains more words, so what little
    signal it carried tracked cluster size rather than recurrence.

    Real dates were sitting unused on 179,533 of that run's documents, spanning 2022-07 to
    2026-08. Persistence is now what it always claimed to be:

      span    - how many months separate the first and last complaint in the theme
      recency - what share of the theme's complaints fall in the last 12 months

    A pain argued over for three years AND still live today scores 10. A single burst last
    month is not persistence, so span carries the larger weight. "Now" is the corpus's own
    newest document, never the wall clock, so re-scoring a finished run cannot quietly age
    its themes.

    Falls back to the keyword proxy for a source that carries no dates at all.
    """
    dates = sorted(d for d in (_parse_date(p.get("created_at")) for p in cluster["pains"]) if d)
    if not dates or corpus_latest is None:
        hits = _term_hits(text, PERSISTENCE_TERMS)
        return round(min(10.0, 3.0 + len(hits) * 1.5), 2), {
            "method": "keyword_fallback", "signals": hits, "dated_pains": 0}
    span_months = (dates[-1] - dates[0]).days / 30.44
    recent_cutoff = corpus_latest.timestamp() - 365 * 86400
    recent = sum(1 for d in dates if d.timestamp() >= recent_cutoff)
    span_factor = _log_scale(span_months, full_at.get("months", 36), cap=1.0)
    recent_factor = recent / len(dates)
    score = round(min(10.0, 1.0 + 6.0 * span_factor + 3.0 * recent_factor), 2)
    return score, {
        "method": "dates",
        "dated_pains": len(dates),
        "undated_pains": len(cluster["pains"]) - len(dates),
        "first": dates[0].date().isoformat(),
        "last": dates[-1].date().isoformat(),
        "span_months": round(span_months, 1),
        "share_last_12_months": round(recent_factor, 2),
    }


def corpus_latest_date(clusters: list):
    """The newest complaint anywhere in the run: the "now" that persistence measures against.

    The corpus's own clock, not the machine's, so the same run re-scored tomorrow scores the
    same.
    """
    latest = None
    for cluster in clusters:
        for pain in cluster["pains"]:
            dt = _parse_date(pain.get("created_at"))
            if dt and (latest is None or dt > latest):
                latest = dt
    return latest


def score_cluster(cluster: dict, weights: dict, corpus_latest=None,
                  full_at: dict = None) -> dict:
    text = "\n".join(_texts(cluster))
    distinct_authors = int(cluster.get("distinct_authors") or 0)
    evidence_count = len(cluster["pains"])
    post_level = sum(1 for p in cluster["pains"] if p.get("source_granularity") == "post")

    full_at = {**DEFAULT_FULL_SCORE_AT, **(full_at or {})}

    pain_intensity, intensity_hits = _term_score(text, INTENSITY_TERMS, base=3.0)
    endorsement, endorsement_evidence = _endorsement(cluster)
    # Distinct authors are the market signal. Repeat posts by the same people count half,
    # and upvotes (silent agreement) ride in as bonus voices -- INSIDE the log, so the
    # ceiling can no longer swallow them the way it swallowed 2,990-upvote posts before.
    voices = (distinct_authors
              + 0.5 * max(0, evidence_count - distinct_authors)
              + endorsement)
    frequency = round(_log_scale(voices, full_at["voices"]), 2)
    willingness_to_pay, wtp_hits = _term_score(text, WTP_TERMS, base=1.0)
    reachability, reach_hits = _term_score(text, REACHABILITY_TERMS, base=2.0)
    if evidence_count:
        reachability = min(10.0, reachability + (post_level / evidence_count) * 2.0)
    recurrence_score, recurrence_evidence = _recurrence(cluster, full_at)
    persistence_score, persistence_evidence = _persistence(
        cluster, text, corpus_latest, full_at)

    parts = {
        "pain_intensity": round(pain_intensity, 2),
        "frequency": round(frequency, 2),
        "willingness_to_pay": round(willingness_to_pay, 2),
        "reachability": round(reachability, 2),
        "recurrence_score": recurrence_score,
    }
    return {
        **parts,
        "persistence_score": persistence_score,  # rank multiplier, NOT in the demand average
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
            "persistence": persistence_evidence,
        },
    }


def demand_run(store, run_id: str, weights: dict, progress=None,
               full_at: dict = None) -> dict:
    clusters = store.get_cluster_details(run_id)
    store.clear_demand(run_id)
    if progress:
        progress(0, len(clusters))
    corpus_latest = corpus_latest_date(clusters)
    dated = 0
    for i, cluster in enumerate(clusters, start=1):
        score = score_cluster(cluster, weights, corpus_latest, full_at)
        if score["scoring_evidence"]["persistence"].get("method") == "dates":
            dated += 1
        store.save_demand_score(run_id, cluster["id"], score)
        if progress:
            progress(i, len(clusters))
    store.set_stage(run_id, 6, "demand_scored")
    return {"scored": len(clusters), "persistence_from_dates": dated,
            "corpus_latest": corpus_latest.isoformat() if corpus_latest else None}
