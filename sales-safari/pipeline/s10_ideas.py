"""Stage 10: generate product idea stubs for top-ranked themes."""


def _clean_label(label: str) -> str:
    label = " ".join((label or "workflow pain").split())
    return label[:80].rstrip(" .")


def ideas_run(store, run_id: str, top_n: int = 5, progress=None) -> dict:
    ranked = store.get_ranked_clusters(run_id)[:top_n]
    details = {c["id"]: c for c in store.get_cluster_details(run_id)}
    store.clear_ideas(run_id)
    made = 0
    if progress:
        progress(0, len(ranked))
    for i, row in enumerate(ranked, start=1):
        cluster = details.get(row["cluster_id"])
        if not cluster:
            if progress:
                progress(i, len(ranked))
            continue
        label = _clean_label(cluster["label"])
        evidence = cluster["pains"][0]
        persona = evidence.get("persona") or "this community"
        workflow = evidence.get("workflow_pain") or evidence.get("complaint") or label
        workaround = evidence.get("workaround") or "manual tracking and forum search"
        title = f"{label} workflow tool"
        pitch = (
            f"For {persona}, a focused workflow tool that replaces {workaround[:90]} "
            f"around: \"{workflow[:140]}\""
        )
        store.save_idea(run_id, cluster["id"], title, pitch,
                        evidence.get("source_permalink") or "")
        made += 1
        if progress:
            progress(i, len(ranked))
    store.set_stage(run_id, 10, "ideas_generated")
    return {"ideas": made}
