"""Stage 6: score demand without considering competition."""
from collections import Counter
from datetime import datetime, timedelta, timezone
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

# Every list below was rebuilt from term frequencies measured over run pbfamily0001's 72,610
# pain texts. The originals were written for a maker / B2B-SaaS niche and never adapted, and
# it showed: 8 of 12 REACHABILITY terms ("makerspace", "etsy", "seller", "hobbyist") fired
# fewer than 50 times in the ENTIRE corpus, and 7 of 18 WTP terms ("invoice", "refund",
# "pricing") were equally dead. `willingness_to_pay` never moved off its base on 90% of
# themes and `pain_intensity` never moved on 68%.
INTENSITY_TERMS = {
    # kept: measured live in this corpus (can't 1,945 / hard 1,820 / hate 1,597)
    "can't", "cannot", "impossible", "frustrating", "frustrated", "hate",
    "broken", "stuck", "waste", "wasting", "pain", "annoying", "problem",
    "issue", "hard", "difficult", "slow",
    # added: how people in THIS corpus say a thing hurts
    "tired of", "sick of", "desperate", "struggle", "struggling", "worst",
    "miserable", "terrible", "awful", "garbage", "trash", "ridiculous",
    "nightmare", "give up", "useless", "hopeless",
    # dropped "manual" and "tedious": B2B workflow words, under 50 hits in 72,610 texts.
    # "gave up" moved to CONSEQUENCE_TERMS: having quit is a cost, not a mood.
}
# Intensity is tiered by CONSEQUENCE. On pbfamily0001 it was driven by "hard", "frustrated"
# and "can't", and 141 ranked themes had nothing else, so a theme that merely sounds annoyed
# tied one whose members say what the pain cost them. Bare "fine" is left out: in the ranked
# themes it reads "you're going to be fine" and "fine dining", never a penalty.
CONSEQUENCE_TERMS = {
    "hours", "lost", "missed", "fined", "fines", "paid a fine", "got a fine", "deadline",
    "cost me", "wasted", "late fee", "penalty", "overstay", "overstayed", "got scammed",
    "lost money", "every day", "every time", "for months", "for years", "gave up",
}

# WTP is graded by TENSE: "anything involving the future is an over-optimistic lie" (The Mom
# Test). Strong is money already changing hands, or a price anchor. Weak is hypothetical or
# generic money talk, which "expensive drinks" shares with "expensive subscription" (how
# "Expensive dates" once reached rank 1). "scam" is in neither tier: it is a fraud complaint.
WTP_STRONG_TERMS = {
    "i paid", "i pay", "paying for", "paid for", "i subscribe", "my subscription",
    "switched from", "hired a", "bought a", "worth paying", "not worth", "premium",
    "paywall", "free trial",
}
# A period needs a number in front of it: bare "a month" in the ranked themes is "once a
# month" and "in about a month", not a price.
WTP_AMOUNT_PATTERNS = (
    r"[$€£]\s?\d+(?:[.,]\d+)*k?",
    r"\d+(?:[.,]\d+)*k?\s?[€£]",
    r"\b\d+(?:[.,]\d+)*k?\s?(?:usd|dollars|bucks|pesos|baht|php|euros?)\b",
    r"\b\d+(?:[.,]\d+)*k?\s?(?:/\s?mo(?:nth)?|a month|per month|per year)\b",
)
WTP_WEAK_TERMS = {
    "would pay", "worth it", "price", "expensive", "cheap", "cost", "costs", "afford",
    "budget", "fee", "fees", "money",
}

# Reachability = can we FIND these buyers and speak to them? In a forum corpus that means
# they name where they gather or which product they already use. The old list asked about
# Etsy sellers and makerspaces, which do not exist here.
REACHABILITY_TERMS = {
    "community", "forum", "forums", "sub", "subreddit", "group", "groups",
    "discord", "telegram", "expat", "expats", "nomad", "nomads", "meetup",
    "app", "apps", "tinder", "bumble", "hinge", "instagram", "youtube",
    "course", "class", "business",
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


def _term_regex(term: str) -> str:
    return r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"


def _term_hits(text: str, terms: set) -> list:
    low = text.lower()
    return sorted(term for term in terms if re.search(_term_regex(term), low))


def _term_score(text: str, terms: set, base: float = 2.0) -> tuple[float, list]:
    hits = _term_hits(text, terms)
    return min(10.0, base + len(hits) * 1.5), hits


# Every text signal of demand is an evidence class matched PER PAIN, and a component reads
# the share of the theme's distinct authors whose own pain carries the class. Matching over
# the concatenated theme text let one member's "premium" light the whole theme.
EVIDENCE_CLASSES = {
    cls: tuple(re.compile(p) for p in patterns) for cls, patterns in {
        "wtp_strong": [_term_regex(t) for t in WTP_STRONG_TERMS] + list(WTP_AMOUNT_PATTERNS),
        "wtp_weak": [_term_regex(t) for t in WTP_WEAK_TERMS],
        "intensity_consequence": [_term_regex(t) for t in CONSEQUENCE_TERMS],
        "intensity_generic": [_term_regex(t) for t in INTENSITY_TERMS],
    }.items()
}
# component -> (floor, points each class adds when every author carries it)
SHARE_COMPONENTS = {
    "willingness_to_pay": (1.0, {"wtp_strong": 6.0, "wtp_weak": 3.0}),
    "pain_intensity": (3.0, {"intensity_consequence": 4.0, "intensity_generic": 3.0}),
}


def _author_key(pain: dict):
    return pain.get("author_hash") or pain.get("id")


def _pain_text(pain: dict) -> str:
    # No persona: it is the extractor's label for the poster, and "frustrated user" alone gave
    # 84 ranked pains on pbfamily0001 an intensity hit their author never wrote. 954 of those
    # 5,376 pains spell "can’t" with a curly apostrophe.
    return " ".join(str(pain.get(k) or "") for k in (
        "complaint", "workflow_pain", "workaround", "wish", "verbatim_span",
    )).lower().replace("\u2019", "'")


def _evidence_table(pains: list) -> tuple[dict, int]:
    authors = set()
    table = {cls: {"authors": set(), "signals": set(), "quotes": []} for cls in EVIDENCE_CLASSES}
    for pain in pains:
        author = _author_key(pain)
        authors.add(author)
        text = _pain_text(pain)
        for cls, patterns in EVIDENCE_CLASSES.items():
            hits = {" ".join(m.group(0).split()) for m in (rx.search(text) for rx in patterns) if m}
            if not hits:
                continue
            row = table[cls]
            quote = _quote(pain)
            if quote and author not in row["authors"] and len(row["quotes"]) < 3:
                row["quotes"].append(quote)
            row["authors"].add(author)
            row["signals"] |= hits
    return table, len(authors)


def _share_score(table: dict, distinct: int, component: str) -> tuple[float, dict]:
    floor, points = SHARE_COMPONENTS[component]
    score, evidence = floor, {}
    for cls, full in points.items():
        row = table[cls]
        share = len(row["authors"]) / distinct if distinct else 0.0
        score += full * share
        evidence[cls] = {
            "authors_with_signal": len(row["authors"]),
            "distinct_authors": distinct,
            "share": round(share, 2),
            "signals": sorted(row["signals"]),
            "quotes": row["quotes"],
        }
    return round(min(10.0, score), 2), {
        "signals": [s for cls in points for s in evidence[cls]["signals"]],
        "quotes": [q for cls in points for q in evidence[cls]["quotes"]][:3],
        **evidence,
    }


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


def _quote(pain: dict):
    quote = str(pain.get("verbatim_span") or "").strip()
    if not quote:
        return None
    return {"quote": quote[:220], "source": pain.get("source_permalink") or "",
            "persona": pain.get("persona") or ""}


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
        "quotes": [q for q in map(_quote, pains) if q][:3],
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


def _momentum(cluster: dict, corpus_latest, full_at: dict) -> tuple[float, dict]:
    """Distinct authors raising the pain in the corpus's last 12 months.

    Persistence rewards age: a pain first seen last year topped out near 4-5 however many
    people raised it, so an emerging pain could not reach the top. Momentum counts the recent
    voices on their own curve, full at half the frequency anchor.
    """
    full = full_at["voices"] / 2
    dated = [(p, d) for p in cluster["pains"] if (d := _parse_date(p.get("created_at")))]
    if not dated or corpus_latest is None:
        return 0.0, {"method": "undated", "recent_authors": 0, "dated_pains": len(dated),
                     "note": "no complaint dates, so no momentum can be measured"}
    cutoff = corpus_latest - timedelta(days=365)
    recent = {_author_key(p) for p, d in dated if d >= cutoff}
    return round(_log_scale(len(recent), full), 2), {
        "method": "dates",
        "recent_authors": len(recent),
        "full_at_authors": full,
        "dated_pains": len(dated),
        "undated_pains": len(cluster["pains"]) - len(dated),
        "window_start": cutoff.date().isoformat(),
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

    table, table_authors = _evidence_table(cluster["pains"])
    pain_intensity, intensity_evidence = _share_score(table, table_authors, "pain_intensity")
    willingness_to_pay, wtp_evidence = _share_score(table, table_authors, "willingness_to_pay")
    endorsement, endorsement_evidence = _endorsement(cluster)
    # Distinct authors are the market signal. Repeat posts by the same people count half,
    # and upvotes (silent agreement) ride in as bonus voices -- INSIDE the log, so the
    # ceiling can no longer swallow them the way it swallowed 2,990-upvote posts before.
    voices = (distinct_authors
              + 0.5 * max(0, evidence_count - distinct_authors)
              + endorsement)
    frequency = round(_log_scale(voices, full_at["voices"]), 2)
    reachability, reach_hits = _term_score(text, REACHABILITY_TERMS, base=2.0)
    if evidence_count:
        reachability = min(10.0, reachability + (post_level / evidence_count) * 2.0)
    recurrence_score, recurrence_evidence = _recurrence(cluster, full_at)
    persistence_score, persistence_evidence = _persistence(
        cluster, text, corpus_latest, full_at)
    momentum, momentum_evidence = _momentum(cluster, corpus_latest, full_at)

    parts = {
        "pain_intensity": round(pain_intensity, 2),
        "frequency": round(frequency, 2),
        "willingness_to_pay": round(willingness_to_pay, 2),
        "reachability": round(reachability, 2),
        "recurrence_score": recurrence_score,
        "momentum": momentum,
    }
    return {
        **parts,
        "persistence_score": persistence_score,  # rank multiplier, NOT in the demand average
        # A source with no dates has no momentum to measure. Leaving the zero in the average
        # would dock an undated theme 2/16 of its demand for a fact about the collector, not
        # the pain, so the term is averaged only when the dates exist.
        "demand_score": _weighted_score(
            parts if momentum_evidence.get("method") == "dates"
            else {k: v for k, v in parts.items() if k != "momentum"},
            {"momentum": 2, **weights}),
        "evidence_count": evidence_count,
        "distinct_authors": distinct_authors,
        "scoring_evidence": {
            "pain_intensity": intensity_evidence,
            "frequency": {
                "distinct_authors": distinct_authors,
                "evidence_count": evidence_count,
                "post_level_evidence": post_level,
                "endorsement": endorsement_evidence,
            },
            "willingness_to_pay": wtp_evidence,
            "reachability": {
                "signals": reach_hits,
                "post_level_evidence": post_level,
            },
            "recurrence": recurrence_evidence,
            "persistence": persistence_evidence,
            "momentum": {"score": momentum, **momentum_evidence},
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
