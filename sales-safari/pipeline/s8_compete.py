"""Stage 8: lightweight competitive intel.

This first pass estimates saturation and persistence from the pain evidence itself.
It intentionally stays separate from demand scoring.
"""

INCUMBENT_TERMS = {
    "excel", "spreadsheet", "google sheets", "airtable", "notion", "zapier",
    "shopify", "etsy", "quickbooks", "salesforce", "hubspot", "slack",
}
PERSISTENCE_TERMS = {
    "still", "again", "keeps", "keep having", "after update", "workaround",
    "manual", "every time", "always", "still can't", "still cannot",
}


def _text(cluster: dict) -> str:
    return "\n".join(
        " ".join(str(p.get(k) or "") for k in (
            "complaint", "workflow_pain", "workaround", "wish", "verbatim_span"))
        for p in cluster["pains"]
    ).lower()


def assess_cluster(cluster: dict) -> dict:
    text = _text(cluster)
    incumbents = sorted({term for term in INCUMBENT_TERMS if term in text})
    persistence_hits = sum(1 for term in PERSISTENCE_TERMS if term in text)
    saturation_score = min(10.0, len(incumbents) * 2.0)
    persistence_score = min(10.0, 3.0 + persistence_hits * 1.5)
    if not incumbents:
        gap = "No obvious incumbent named in evidence; competition needs manual discovery."
    elif persistence_hits:
        gap = "Evidence names incumbents but complaints persist, suggesting unresolved workflow gaps."
    else:
        gap = "Incumbents appear in evidence; verify differentiation before pursuing."
    return {
        "incumbent_count": len(incumbents),
        "saturation_score": round(saturation_score, 2),
        "persistence_score": round(persistence_score, 2),
        "gap_summary": gap,
    }


def compete_run(store, run_id: str, sources=None, progress=None) -> dict:
    clusters = store.get_cluster_details(run_id)
    store.clear_competition(run_id)
    if progress:
        progress(0, len(clusters))
    for i, cluster in enumerate(clusters, start=1):
        store.save_competitive_intel(run_id, cluster["id"], assess_cluster(cluster))
        if progress:
            progress(i, len(clusters))
    store.set_stage(run_id, 8, "competition_checked")
    return {"checked": len(clusters)}
