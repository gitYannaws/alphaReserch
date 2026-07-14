"""Stage 12: write a Markdown report with permalinked evidence."""
from pathlib import Path


def _fmt(n):
    if n is None:
        return "-"
    return f"{n:.2f}" if isinstance(n, float) else str(n)


def _signals(row, dim):
    ev = row.get("scoring_evidence") or {}
    signals = ((ev.get(dim) or {}).get("signals") or [])[:5]
    return ", ".join(signals) if signals else "none"


def report_run(store, run_id: str, out_dir: str = "reports", progress=None) -> dict:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    report_path = Path(out_dir) / f"{run_id}.md"
    ranked = store.get_ranked_clusters(run_id, include_dropped=True)
    ideas = store.get_ideas(run_id)
    total = len(ranked) + len(ideas) + 1
    done = 0
    if progress:
        progress(0, total)
    details = {c["id"]: c for c in store.get_cluster_details(run_id)}
    competitors = {}
    for c in store.get_competitors(run_id):
        competitors.setdefault(c["cluster_id"], []).append(c)
    reviews_by_comp = {}
    for r in store.get_reviews(run_id):
        reviews_by_comp.setdefault(r["competitor_id"], []).append(r)


    def _comp_lines(cluster_id):
        comps = competitors.get(cluster_id)
        if not comps:
            return []
        out = ["", "**Competitors:**"]
        for c in comps:
            name = f"[{c['name']}]({c['url']})" if c.get("url") else c["name"]
            bits = [b for b in (c.get("category"), c.get("note")) if b]
            tail = f" - {'; '.join(bits)}" if bits else ""
            dom = f" (reviews: {c['review_domain']})" if c.get("review_domain") else ""
            revs = reviews_by_comp.get(c["id"], [])
            count = f" - {len(revs)} x 1-2* reviews" if revs else ""
            out.append(f"- {name}{tail}{dom}{count}")
            for rv in revs[:3]:  # top low-star complaints = incumbent gaps
                title = f"**{rv['title']}** - " if rv.get("title") else ""
                out.append(f"  - {rv['rating']}*: {title}{(rv.get('body') or '')[:180]}")
        return out

    lines = [
        f"# Sales Safari Report - {run_id}",
        "",
        "## Ranked Themes",
        "",
    ]
    if not ranked:
        lines.extend([
            "No ranked themes were produced for this run.",
            "",
            "This usually means pain extraction produced no pains, or clustering produced no themes.",
            "",
        ])
    for row in ranked:
        status = "dropped" if row["dropped"] else f"rank {row['rank']}"
        lines.extend([
            f"### {status}: {row['label']}",
            "",
            f"- Rank score: {_fmt(row['rank_score'])}",
            f"- Demand: {_fmt(row['demand_score'])}",
            f"- Demand parts: intensity {_fmt(row.get('pain_intensity'))}, "
            f"frequency {_fmt(row.get('frequency'))}, WTP {_fmt(row.get('willingness_to_pay'))}, "
            f"reach {_fmt(row.get('reachability'))}, recurrence {_fmt(row.get('recurrence_score'))}",
            f"- Rank formula: demand {_fmt(row['demand_score'])} x persistence {_fmt(row['persistence_score'])} "
            f"/ (1 + saturation {_fmt(row['saturation_score'])}) x solvable weight "
            f"{_fmt(row.get('solvable_weight'))}",
            f"- Persistence: {_fmt(row['persistence_score'])}",
            f"- Saturation: {_fmt(row['saturation_score'])}",
            f"- Authors: {row['distinct_authors']}",
            f"- Scoring signals: intensity [{_signals(row, 'pain_intensity')}]; "
            f"WTP [{_signals(row, 'willingness_to_pay')}]",
        ])
        if row["filter_reasons"]:
            lines.append(f"- Warning flags: {', '.join(row['filter_reasons'])}")
        if row["gap_summary"]:
            lines.append(f"- Competition note: {row['gap_summary']}")
        cluster = details.get(row["cluster_id"])
        if cluster and cluster["pains"]:
            pain = cluster["pains"][0]
            src = pain.get("source_permalink") or ""
            lines.append(f"- Evidence: [{pain.get('verbatim_span', '')[:180]}]({src})")
        lines.extend(_comp_lines(row["cluster_id"]))
        lines.append("")
        done += 1
        if progress:
            progress(done, total)

    lines.extend(["## Ideas And Kill Tests", ""])
    if not ideas:
        lines.extend([
            "No ideas were generated because there were no non-dropped ranked themes.",
            "",
        ])
    for idea in ideas:
        lines.extend([
            f"### {idea['title']}",
            "",
            idea["pitch"],
            "",
            f"- Evidence: {idea['evidence_permalink']}",
            f"- Kill test: {idea.get('kill_test') or ''}",
            f"- Metric: {idea.get('metric') or ''}",
            f"- Threshold: {idea.get('threshold') or ''}",
            f"- Timeframe: {idea.get('timeframe') or ''}",
            f"- Channel: {idea.get('channel') or ''}",
        ])
        lines.extend(_comp_lines(idea.get("cluster_id")))
        lines.append("")
        done += 1
        if progress:
            progress(done, total)

    report_path.write_text("\n".join(lines), encoding="utf-8")
    done += 1
    if progress:
        progress(done, total)
    store.save_report(run_id, str(report_path))
    store.set_stage(run_id, 12, "reported")
    return {"path": str(report_path)}
