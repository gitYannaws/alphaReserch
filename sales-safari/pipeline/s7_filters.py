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
# The lookaheads keep "sub $700" (a price) and "post-covid" (a date) out of the venue.
PLATFORM_META_PATTERNS = (
    r"\bsub(reddit)?s?\b(?! ?[$€£\d])", r"\breddit(ors?)?\b", r"\bmod(erator)?s?\b",
    r"\bdownvot", r"\bupvot", r"\bkarma\b", r"\bflair\b", r"\bshadow ?ban",
    r"\bthis (sub|forum|group|thread|post|board|community)\b",
    r"\bcomment section\b", r"\btrolls?\b", r"\bbanned\b", r"\bthe algorithm\b",
    r"\bmy (feed|home ?page|timeline)\b", r"\bthe rules\b", r"\bdeleted\b",
    r"\bspam(ming|mer)?\b", r"\bposts?\b(?!-)", r"\bthreads?\b", r"\bop\b", r"\bcomments?\b",
    r"\blurk", r"\boff[ -]topic\b", r"\bderail", r"\bhome ?page\b", r"\bfeed\b",
    r"\bpunctuation\b", r"\bparagraphs?\b", r"\bwall of text\b", r"\btl;?dr\b",
    r"\bblock(ed|ing)? (him|her|them|you)\b",
)
# Posters arguing with EACH OTHER. "frustration with baseless accusations and derailment of
# topics" ranked 11 on pbfamily0001 at a platform share of 0.00: its members name the other
# poster, not the venue. No product addresses that.
ARGUMENT_CHATTER_PATTERNS = (
    r"\byou('re| are) (wasting|lying|delusional|coping|wrong|an? )", r"\byour kind\b",
    r"\bcope\b", r"\bdumbass\b", r"\bidiot\b", r"\bstfu\b", r"\bstop assuming\b",
    r"\bmake assumptions\b", r"\bno idea what you\b", r"\bcounterargument", r"\bstrawman\b",
    r"\bgaslight", r"\btroll(s|ing)?\b", r"\bwasting (your|our|their|my) time\b",
    r"\bdon'?t bother\b",
    # "lol" is everywhere; only aimed at someone is it a jab.
    r"(?s)^(?=.*\blol\b).*\byou(r|'?re)?\b",
)
# Tags judged per MEMBER, then by share. A FILTER_PATTERNS tag fires on one hit anywhere in
# the theme, which is fine for a caveat ("someone mentioned a visa") but wrong for a tag that
# can DROP: one post saying "this sub" must not brand a whole market theme.
SHARE_TAGS = {
    # 0.5 let "spamming issues" (0.29) and "writing is difficult to read" (0.17) through.
    "platform_meta": (PLATFORM_META_PATTERNS, 0.3),
    "argument_chatter": (ARGUMENT_CHATTER_PATTERNS, 0.4),
}


def _pain_text(pain: dict) -> str:
    return " ".join(str(pain.get(k) or "") for k in (
        "complaint", "workflow_pain", "workaround", "wish", "verbatim_span",
    )).lower().replace("\u2019", "'")


def share_tag_share(cluster: dict, tag: str) -> float:
    """Fraction of a theme's pains that carry `tag`'s patterns."""
    patterns, _min_share = SHARE_TAGS[tag]
    pains = cluster.get("pains") or []
    if not pains:
        return 0.0
    hits = sum(1 for p in pains if any(re.search(pat, _pain_text(p)) for pat in patterns))
    return hits / len(pains)


def platform_meta_share(cluster: dict) -> float:
    return share_tag_share(cluster, "platform_meta")


def evaluate_cluster(cluster: dict, enabled_filters) -> list:
    text = "\n".join(map(_pain_text, cluster["pains"]))
    reasons = []
    for name in enabled_filters:
        if name in SHARE_TAGS:
            hit = share_tag_share(cluster, name) >= SHARE_TAGS[name][1]
        else:
            hit = any(re.search(pattern, text) for pattern in FILTER_PATTERNS.get(name, ()))
        if hit:
            reasons.append(name)
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
