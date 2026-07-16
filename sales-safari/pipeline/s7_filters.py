"""Stage 7: advisory filter tags.

Historically this stage could drop clusters outright. We now keep the matching
reasons as metadata only, so later ranking/reporting can show the caveats
without excluding any cluster from the pipeline.
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
