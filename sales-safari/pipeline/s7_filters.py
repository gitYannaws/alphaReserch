"""Advisory warning tags for a theme (regex, free).

No longer its own pipeline stage: `softfilter_run` (stage 7b) calls `evaluate_cluster` and
stores the hits as `soft_filters.warnings`, so warnings + software-fit are one advisory pass.
`filters_run` is retained only for the standalone `analyze.py` path.

Historically this could drop clusters outright. Reasons are metadata only - ranking/reporting
show the caveat without excluding the theme.

NOTE on coverage: the first four groups are B2B-SaaS shaped and fired on just 5 of 920 themes
(0.5%) against an expat/travel/dating corpus. The groups below them are the domain-relevant
counterparts. Patterns are matched against the cluster's pain text, so they only catch what
posters actually say.
"""

import re

FILTER_PATTERNS = {
    "requires_soc2_hipaa": (
        r"\bhipaa\b",
        r"\bsoc\s*2\b",
        r"\bpatients?\b",
        r"\bmedical records?\b",
        r"\bprotected health information\b",
        r"\bphi\b",
    ),
    "two_sided_marketplace": (
        r"\bmarketplace\b",
        r"\bbuyers and sellers\b",
        r"\btwo[-\s]sided\b",
    ),
    "closed_api_dependency": (
        r"\bclosed api\b",
        r"\bprivate api\b",
        r"\bno api\b",
        r"\bapi access\b",
        r"\blocked down\b",
    ),
    "regulated_liability": (
        r"\blegal advice\b",
        r"\bfinancial advice\b",
        r"\btax advice\b",
        r"\bregulated\b",
        r"\bliability\b",
    ),
    # ---- domain-relevant (expat / travel / nomad / dating corpora) ----
    "visa_immigration_process": (
        r"\bvisas?\b",
        r"\bimmigration\b",
        r"\bresidency\b",
        r"\bwork permit\b",
        r"\bembassy\b",
        r"\bconsulate\b",
        r"\bborder run\b",
    ),
    "banking_or_payments": (
        r"\bbank account\b",
        r"\bwire transfer\b",
        r"\bremittance\b",
        r"\bcash only\b",
        r"\bexchange rate\b",
        r"\bwestern union\b",
    ),
    "requires_in_person": (
        r"\bin person\b",
        r"\bface to face\b",
        r"\bshow up\b",
        r"\bnotari[sz]e\b",
        r"\bappointment\b",
    ),
    "language_barrier": (
        r"\blanguage barrier\b",
        r"\bdon'?t speak\b",
        r"\btranslation\b",
        r"\benglish speaking\b",
    ),
    "physical_logistics": (
        r"\bshipping\b",
        r"\bcustoms\b",
        r"\bluggage\b",
        r"\bapartment\b",
        r"\bhousing\b",
        r"\blandlord\b",
    ),
}


# A theme about the VENUE, not about the market. A forum corpus is full of these: people on
# Reddit complain about Reddit. Run pbfamily0001 ranked "moderator abuse and unfair bans" at
# 8, "subreddit toxicity" at 11 and "Posts receive very little engagement" at 12; after the
# re-run, "frequent unwanted posts on home page" sat at 4 and "Reddit showing disgusting
# content" at 10. Real complaints, but they are not this market's pain.
PLATFORM_META_PATTERNS = (
    r"\bsub(reddit)?s?\b", r"\breddit(ors?)?\b", r"\bmod(erator)?s?\b",
    r"\bdownvot", r"\bupvot", r"\bkarma\b", r"\bflair\b", r"\bshadow ?ban",
    r"\bthis (sub|forum|group|thread|post|board|community)\b",
    r"\bcomment section\b", r"\btrolls?\b", r"\bbanned\b", r"\bthe algorithm\b",
    r"\bmy (feed|home ?page|timeline)\b", r"\bthe rules\b", r"\bdeleted\b",
)
# Judged per MEMBER, then by share. The other tags fire on a single hit anywhere in the
# theme's text, which is fine for a caveat ("someone mentioned a visa") but wrong here: one
# post saying "this sub" must not brand a whole market theme as venue chatter. This tag can
# be configured to DROP, so it has to be right about the theme, not about one post.
PLATFORM_META_MIN_SHARE = 0.5


def _pain_text(pain: dict) -> str:
    return " ".join(str(pain.get(k) or "") for k in (
        "complaint", "workflow_pain", "workaround", "wish", "verbatim_span")).lower()


def platform_meta_share(cluster: dict) -> float:
    """Fraction of a theme's pains that are talking about the forum itself."""
    pains = cluster.get("pains") or []
    if not pains:
        return 0.0
    hits = sum(1 for p in pains
               if any(re.search(pat, _pain_text(p)) for pat in PLATFORM_META_PATTERNS))
    return hits / len(pains)


def _cluster_text(cluster: dict) -> str:
    return "\n".join(
        " ".join(str(p.get(k) or "") for k in (
            "complaint", "workflow_pain", "workaround", "wish", "verbatim_span"))
        for p in cluster["pains"]
    ).lower()


def evaluate_cluster(cluster: dict, enabled_filters) -> list:
    text = _cluster_text(cluster)
    reasons = []
    for name in enabled_filters:
        if name == "platform_meta":
            if platform_meta_share(cluster) >= PLATFORM_META_MIN_SHARE:
                reasons.append(name)
            continue
        for pattern in FILTER_PATTERNS.get(name, ()):
            if re.search(pattern, text):
                reasons.append(name)
                break
    return reasons


def filters_run(store, run_id: str, enabled_filters, progress=None) -> dict:
    clusters = store.get_cluster_details(run_id)
    store.clear_filters(run_id)
    flagged = 0
    if progress:
        progress(0, len(clusters))
    for i, cluster in enumerate(clusters, start=1):
        reasons = evaluate_cluster(cluster, enabled_filters)
        flagged += 1 if reasons else 0
        store.save_filter_result(run_id, cluster["id"], False, reasons)
        if progress:
            progress(i, len(clusters))
    store.set_stage(run_id, 7, "filtered")
    return {"checked": len(clusters), "flagged": flagged, "dropped": 0}
