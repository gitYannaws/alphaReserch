"""Run ONE extractor over the frozen bench sample; score with the production span-gate.

Usage: run_model.py <qwen2.5|qwen3|sonnet|haiku|codex>

- Gate helpers + PROMPT_HEADER are IMPORTED from pipeline.extract -> byte-identical to prod.
- Evaluated in-memory. Nothing is written to the pains table.
- Real model calls only. Failures (429/timeouts) are recorded, never faked.
Appends its result under the model key in db/finish-bench-results.json.
"""
import json, subprocess, sys, tempfile, time, os
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import yaml
from pipeline.extract import (PROMPT_HEADER, _span_bounds, _clean_field,
                              _parse_json_array, _call_local, _call_codex)

SAMPLE = Path("db/finish-bench-sample.json")
RESULTS = Path("db/finish-bench-results.json")
CFG = yaml.safe_load(open("config.yaml", encoding="utf-8"))["extract"]


def call_claude(prompt, model, timeout=180):
    """Claude -p; returns (text, cost_usd, resolved_model). Raises on error."""
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json"],
        input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, shell=False)
    w = {}
    try:
        w = json.loads(proc.stdout)
    except Exception:
        pass
    if proc.returncode != 0 or w.get("is_error"):
        detail = w.get("result") or w.get("message") or proc.stderr.strip() or "unknown"
        if w.get("api_error_status"):
            detail = f"API {w['api_error_status']}: {detail}"
        raise RuntimeError(detail)
    resolved = ""
    mu = w.get("modelUsage") or {}
    if mu:
        resolved = sorted(mu.keys())[0]
    return w.get("result", ""), float(w.get("total_cost_usd") or 0.0), resolved


def run(model_key):
    s = json.loads(SAMPLE.read_text(encoding="utf-8"))
    batch = s["batch_size"]
    known = s["known_pain_docs"]
    rand = s["random_no_pain_docs"]
    docs = known + rand
    by_id = {d["id"]: d for d in docs}
    known_ids = {d["id"] for d in known}
    rand_ids = {d["id"] for d in rand}
    existing = {d["id"]: set(d.get("existing_spans") or []) for d in known}

    kept, dropped = [], []
    drop_reasons = {}
    field_counts = {k: 0 for k in ("complaint", "workflow_pain", "workaround", "wish", "persona")}
    raw_items = 0
    seconds = []
    failures = []
    cost = 0.0
    resolved_model = ""

    n_batches = (len(docs) + batch - 1) // batch
    for bi in range(0, len(docs), batch):
        chunk = docs[bi:bi + batch]
        payload = [{"id": d["id"], "title": d["title"], "text": d["text"]} for d in chunk]
        prompt = PROMPT_HEADER + json.dumps(payload, ensure_ascii=False)
        t0 = time.time()
        try:
            if model_key in ("sonnet", "haiku"):
                raw, c, rm = call_claude(prompt, model_key, CFG.get("claude_timeout", 180))
                cost += c
                resolved_model = rm or resolved_model
            elif model_key == "codex":
                raw = _call_codex(prompt, cfg=CFG.get("codex", {}), timeout=CFG.get("codex_timeout", 300))
            else:  # local: qwen2.5 -> extract.qwen block, qwen3 -> extract.qwen3
                block = {"qwen2.5": "qwen", "qwen3": "qwen3"}.get(model_key, model_key)
                raw = _call_local(prompt, cfg=CFG[block], timeout=CFG.get("local_timeout", 300))
            items = _parse_json_array(raw)
        except Exception as e:
            failures.append(str(e)[:400])
            print(f"MODEL {model_key} batch {bi//batch+1}/{n_batches} FAILED: {str(e)[:120]}", flush=True)
            continue
        dt = time.time() - t0
        seconds.append(dt)
        raw_items += len(items)
        bkept = 0
        for it in items:
            doc = by_id.get(it.get("post_id"))
            span = _clean_field(it.get("verbatim_span"))
            bounds = _span_bounds(doc["text"], span) if doc and span else None
            if not doc or not span or not bounds:
                dropped.append({"doc": it.get("post_id"), "reason": "bad_span", "span": span[:60]})
                drop_reasons["bad_span"] = drop_reasons.get("bad_span", 0) + 1
                continue
            complaint = _clean_field(it.get("complaint"))
            workflow_pain = _clean_field(it.get("workflow_pain"))
            workaround = _clean_field(it.get("workaround"))
            wish = _clean_field(it.get("wish"))
            persona = _clean_field(it.get("persona"))
            if not any((complaint, workflow_pain, wish)):
                dropped.append({"doc": doc["id"], "reason": "no_core_field"})
                drop_reasons["no_core_field"] = drop_reasons.get("no_core_field", 0) + 1
                continue
            for k, v in (("complaint", complaint), ("workflow_pain", workflow_pain),
                         ("workaround", workaround), ("wish", wish), ("persona", persona)):
                if v:
                    field_counts[k] += 1
            kept.append({"doc": doc["id"], "span": span,
                         "overlap": span in existing.get(doc["id"], set())})
            bkept += 1
        print(f"MODEL {model_key} batch {bi//batch+1}/{n_batches}: raw={len(items)} kept={bkept} sec={dt:.1f}", flush=True)

    kept_docs = {k["doc"] for k in kept}
    core_total = field_counts["complaint"] + field_counts["workflow_pain"] + field_counts["wish"]
    span_lens = [len(k["span"]) for k in kept]
    result = {
        "resolved_model": resolved_model or model_key,
        "raw_items": raw_items,
        "kept": len(kept),
        "dropped": len(dropped),
        "drop_reasons": drop_reasons,
        "docs_with_pain": len(kept_docs),
        "known_pain_docs_found": len(kept_docs & known_ids),
        "known_pain_doc_coverage": round(len(kept_docs & known_ids) / len(known_ids), 3),
        "random_no_pain_docs_flagged": len(kept_docs & rand_ids),
        "random_flag_rate": round(len(kept_docs & rand_ids) / len(rand_ids), 3),
        "exact_overlap_with_existing_spans": sum(1 for k in kept if k["overlap"]),
        "field_counts": field_counts,
        "avg_core_fields_per_item": round(core_total / len(kept), 2) if kept else 0,
        "avg_span_chars": round(sum(span_lens) / len(span_lens), 1) if span_lens else 0,
        "seconds_total": round(sum(seconds), 1),
        "seconds_avg_call": round(sum(seconds) / len(seconds), 2) if seconds else 0,
        "batches_ok": len(seconds),
        "batches_failed": len(failures),
        "cost_usd": round(cost, 4) if model_key in ("sonnet", "haiku") else None,
        "failures": failures,
        "sample_kept": kept[:12],
        "sample_dropped": dropped[:12],
    }
    allr = {}
    if RESULTS.exists():
        allr = json.loads(RESULTS.read_text(encoding="utf-8"))
    allr.setdefault("_meta", {"seed": s["seed"], "sample_size": len(docs),
                              "known": len(known), "random": len(rand), "batch": batch})
    allr[model_key] = result
    RESULTS.write_text(json.dumps(allr, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nDONE {model_key}: kept={result['kept']} "
          f"known_cov={result['known_pain_doc_coverage']} "
          f"rand_flag={result['random_flag_rate']} "
          f"bad_span={drop_reasons.get('bad_span',0)} "
          f"fail_batches={len(failures)} cost={result['cost_usd']}", flush=True)


if __name__ == "__main__":
    run(sys.argv[1])
