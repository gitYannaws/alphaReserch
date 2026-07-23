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
