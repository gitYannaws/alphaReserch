"""Score an extractor against the HUMAN gold labels. The non-circular metric.

    python bench/score_gold.py incumbent          # what the pains table already holds
    python bench/score_gold.py qwen2.5
    python bench/score_gold.py qwen2.5 qwen3 --union

`incumbent` scores the pains ALREADY in the DB for the gold docs - no model calls. That is
what answers the open question in docs/signal-improvement-plan.md: is the low pain yield an
extraction miss, or is the collection genuinely pain-free?

WHY THE NUMBERS DIFFER FROM run_model.py
----------------------------------------
run_model.py scores against the `pains` table, i.e. against the previous extractor's
output, so "recall" there means "agreement with the old extractor" and its "false
positives" may be real pains the old extractor missed. This scores against human labels,
so precision and recall mean what they say.

REWEIGHTING
-----------
The gold sample oversamples long docs (they hold most of the pain). Each doc carries its
stratum weight, so weighted rates estimate the whole extracted population rather than the
sample. Unweighted sample rates are printed alongside for transparency.

Nothing is written to the pains table.
"""
import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import sqlite3

import yaml

from pipeline.extract import (PROMPT_HEADER, _call_codex, _call_local, _clean_field,
                              _parse_json_array, _span_bounds, _verify_context,
                              classify_candidates)

SAMPLE = Path("db/gold-sample.json")
LABELS = Path("db/gold-labels.json")
OUT = Path("db/gold-results.json")
CFG_ALL = yaml.safe_load(open("config.yaml", encoding="utf-8"))
CFG = CFG_ALL["extract"]
LOCAL_BLOCK = {"qwen2.5": "qwen", "qwen3": "qwen3"}


def wilson(k: int, n: int):
    """95% CI for a proportion. With ~50 gold pain docs the interval is wide - printing it
    stops us reading a 3-point model difference as real when it is noise."""
    if not n:
        return (0.0, 0.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - m), min(1.0, c + m))


def call(model_key: str, prompt: str) -> str:
    # Any codex* key routes to its own config block (codex, codex_sol, ...), so a new
    # model/effort combo is a config edit rather than a code change.
    if model_key.startswith("codex"):
        block = CFG.get(model_key)
        if block is None:
            raise SystemExit(f"no `extract.{model_key}` block in config.yaml")
        return _call_codex(prompt, cfg=block, timeout=CFG.get("codex_timeout", 300))
    if model_key in ("sonnet", "haiku"):
        from pipeline.extract import _call_claude
        return _call_claude(prompt, timeout=CFG.get("claude_timeout", 180), model=model_key)
    return _call_local(prompt, cfg=CFG[LOCAL_BLOCK.get(model_key, model_key)],
                       timeout=CFG.get("local_timeout", 300))


def flags_from_incumbent(docs: list) -> dict:
    """Pains already stored for these docs - the extractor that actually ran in production."""
    con = sqlite3.connect(CFG_ALL["db_path"])
    out = {}
    for d in docs:
        rows = con.execute(
            "SELECT verbatim_span FROM pains WHERE document_id=?", (d["id"],)).fetchall()
        if rows:
            out[d["id"]] = [{"span": r[0] or ""} for r in rows]
    con.close()
    return out


def flags_from_model(model_key: str, docs: list, batch: int) -> dict:
    by_id = {d["id"]: d for d in docs}
    out: dict = {}
    n_batches = (len(docs) + batch - 1) // batch
    for bi in range(0, len(docs), batch):
        chunk = docs[bi:bi + batch]
        payload = [{"id": d["id"], "title": d["title"], "text": d["text"]} for d in chunk]
        t0 = time.time()
        try:
            items = _parse_json_array(call(model_key, PROMPT_HEADER + json.dumps(payload, ensure_ascii=False)))
        except Exception as e:
            print(f"  {model_key} batch {bi//batch+1}/{n_batches} FAILED: {str(e)[:120]}", flush=True)
            continue
        n = 0
        for it in items:
            doc = by_id.get(it.get("post_id"))
            span = _clean_field(it.get("verbatim_span"))
            if not doc or not span or not _span_bounds(doc["text"], span):
                continue  # production span gate
            if not any(_clean_field(it.get(k)) for k in ("complaint", "workflow_pain", "wish")):
                continue  # production core-field gate
            summary = " | ".join(_clean_field(it.get(k)) for k in
                                  ("complaint", "workflow_pain", "wish") if _clean_field(it.get(k)))
            out.setdefault(doc["id"], []).append({"span": span, "summary": summary})
            n += 1
        print(f"  {model_key} batch {bi//batch+1}/{n_batches}: raw={len(items)} "
              f"kept={n} sec={time.time()-t0:.1f}", flush=True)
    return out


def apply_verify(base_flags: dict, docs: list) -> dict:
    """Run stage-3b verify over an extractor's candidate flags and keep only survivors.
    Measures the 3a+3b PIPELINE against gold labels: a doc stays flagged only if at least
    one of its candidate pains passes the keep_types policy."""
    by_id = {d["id"]: d for d in docs}
    cands, idmap = [], {}
    for did, spans in base_flags.items():
        doc = by_id.get(did)
        if not doc:
            continue
        for j, sp in enumerate(spans):
            cid = f"{did}#{j}"
            idmap[cid] = (did, sp)
            cands.append({"id": cid, "span": sp["span"], "summary": sp.get("summary", ""),
                          "title": doc.get("title", ""),
                          "context": _verify_context(doc["text"], doc["text"].find(sp["span"]),
                                                     sp["span"])})
    verdicts = classify_candidates(cands, CFG_ALL.get("verify", {}), CFG,
                                   progress=lambda d, t, k: print(
                                       f"  verify {d}/{t} candidates, {k} kept", flush=True))
    out, rejected = {}, 0
    for cid, (did, sp) in idmap.items():
        v = verdicts.get(cid, {"verified": 1, "pain_type": "unjudged"})
        if v.get("verified", 1):
            out.setdefault(did, []).append(sp)
        else:
            rejected += 1
    print(f"  verify: {len(cands)} candidates -> {rejected} rejected, "
          f"{sum(len(v) for v in out.values())} kept", flush=True)
    return out


def overlaps(a: str, b: str) -> bool:
    """Do two spans refer to the same passage? Substring either way = same evidence."""
    a, b = (a or "").strip(), (b or "").strip()
    return bool(a) and bool(b) and (a in b or b in a)


def score(flags: dict, labels: list, weights: dict) -> dict:
    tp = fp = fn = tn = 0
    wtp = wfp = wfn = 0.0
    span_hit = span_tot = 0
    per_bucket = {}
    misses, false_alarms = [], []

    for lab in labels:
        did, w = lab["doc_id"], weights[lab["bucket"]]
        truth, pred = bool(lab["has_pain"]), did in flags
        b = per_bucket.setdefault(lab["bucket"], {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
        if truth and pred:
            tp += 1; wtp += w; b["tp"] += 1
            span_tot += 1
            if any(overlaps(p["span"], g) for p in flags[did] for g in (lab["spans"] or [])):
                span_hit += 1
        elif truth and not pred:
            fn += 1; wfn += w; b["fn"] += 1
            misses.append({"doc_id": did, "bucket": lab["bucket"], "gold_spans": lab["spans"]})
        elif not truth and pred:
            fp += 1; wfp += w; b["fp"] += 1
            false_alarms.append({"doc_id": did, "bucket": lab["bucket"],
                                 "model_spans": [p["span"] for p in flags[did]]})
        else:
            tn += 1; b["tn"] += 1

    rec = tp / (tp + fn) if (tp + fn) else 0.0
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    return {
        "n_labelled": len(labels),
        "n_gold_pain": tp + fn,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "recall": round(rec, 3),
        "recall_ci95": [round(x, 3) for x in wilson(tp, tp + fn)],
        "precision": round(prec, 3),
        "precision_ci95": [round(x, 3) for x in wilson(tp, tp + fp)],
        "f1": round(2 * prec * rec / (prec + rec), 3) if (prec + rec) else 0.0,
        "recall_weighted": round(wtp / (wtp + wfn), 3) if (wtp + wfn) else 0.0,
        "precision_weighted": round(wtp / (wtp + wfp), 3) if (wtp + wfp) else 0.0,
        "span_agreement": round(span_hit / span_tot, 3) if span_tot else None,
        "per_bucket": per_bucket,
        "misses": misses,
        "false_alarms": false_alarms,
    }


def main(model_keys: list, union: bool, sample_path: Path = SAMPLE, labels_path: Path = LABELS):
    if not labels_path.exists():
        raise SystemExit(f"{labels_path} not found. Label db/gold-sample.json with bench/label.html first.")
    s = json.loads(sample_path.read_text(encoding="utf-8"))
    gold = json.loads(labels_path.read_text(encoding="utf-8"))
    if gold.get("sample_seed") != s.get("seed"):
        raise SystemExit(f"labels are for seed {gold.get('sample_seed')}, sample is seed {s.get('seed')}")

    labels = gold["labels"]
    labels_hash = hashlib.sha256(json.dumps(
        sorted((l["doc_id"], bool(l["has_pain"]), tuple(l.get("spans") or [])) for l in labels)
    ).encode("utf-8")).hexdigest()[:16]
    labelled_ids = {l["doc_id"] for l in labels}
    docs = [d for d in s["docs"] if d["id"] in labelled_ids]
    weights = {st["bucket"]: st["weight"] for st in s["strata"]}
    n_pain = sum(1 for l in labels if l["has_pain"])
    print(f"gold: {len(labels)} labelled docs, {n_pain} with pain "
          f"({n_pain/len(labels)*100:.0f}% of sample)")
    if s.get("scope_warning"):
        print(f"\n!! SCOPE: {s['scope_warning']}")
    print()

    def base_flags(mk):
        return flags_from_incumbent(docs) if mk == "incumbent" else flags_from_model(mk, docs, 5)

    runs = {}
    for mk in model_keys:
        print(f"--- {mk} ---")
        if mk.endswith("+verify"):
            # 3a+3b pipeline: run the base extractor, then keep only pains verify accepts.
            runs[mk] = apply_verify(base_flags(mk[:-len("+verify")]), docs)
        else:
            runs[mk] = base_flags(mk)

    results = {k: score(f, labels, weights) for k, f in runs.items()}

    # Reuse scores for models already run against THESE EXACT labels. Without this, adding
    # one model to the table re-bills every cloud model in it. Keyed on a fingerprint of the
    # labels: edit a single verdict and the cache is dropped rather than silently mixing
    # scores from two different ground truths.
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
            if prev.get("labels_hash") == labels_hash and prev.get("sample_seed") == s["seed"]:
                # UNION rows cache too: the hash match proves their members were scored
                # against this same ground truth, so the pooled score is still valid. They
                # just cannot be re-unioned, which `runs[k] = None` below enforces.
                for k, v in (prev.get("results") or {}).items():
                    if k not in results:
                        results[k] = v
                        runs[k] = None  # cached: flags not available for re-unioning
            elif prev.get("results"):
                print("  (labels changed since last run - discarding cached scores)")
        except (ValueError, KeyError):
            pass

    if union:
        keys = [k.strip() for k in union.split(",")]
        bad = [k for k in keys if k not in runs]
        if bad:
            raise SystemExit(f"--union names {bad} which were not run this invocation; "
                             f"pass them as models too (cached scores cannot be unioned)")
        stale = [k for k in keys if runs[k] is None]
        if stale:
            raise SystemExit(f"--union needs live flags for {stale}, but they came from cache; "
                             f"re-run them in this invocation")
        merged: dict = {}
        for k in keys:
            for did, v in runs[k].items():
                merged.setdefault(did, []).extend(v)
        results["UNION(" + "+".join(keys) + ")"] = score(merged, labels, weights)

    OUT.write_text(json.dumps({"sample_seed": s["seed"], "gold_pain_docs": n_pain,
                               "labels_hash": labels_hash, "results": results},
                              ensure_ascii=False, indent=1), encoding="utf-8")

    short = {k: (k if len(k) <= 22 else k[:19] + "...") for k in results}
    print("\n" + "=" * 78)
    print(f"{'model':<22} {'recall':>16} {'precision':>16} {'F1':>6} {'span':>6}")
    for k, r in results.items():
        rc = f"{r['recall']*100:.0f}% [{r['recall_ci95'][0]*100:.0f}-{r['recall_ci95'][1]*100:.0f}]"
        pc = f"{r['precision']*100:.0f}% [{r['precision_ci95'][0]*100:.0f}-{r['precision_ci95'][1]*100:.0f}]"
        sa = f"{r['span_agreement']*100:.0f}%" if r["span_agreement"] is not None else "-"
        print(f"{short[k]:<22} {rc:>16} {pc:>16} {r['f1']*100:>5.0f}% {sa:>6}")
    print(f"\n{'model':<22} {'recall_w':>9} {'prec_w':>9}   (reweighted to the extracted population)")
    for k, r in results.items():
        print(f"{short[k]:<22} {r['recall_weighted']*100:>8.0f}% {r['precision_weighted']*100:>8.0f}%")
    print(f"\n[] = 95% CI - it stays wide at this sample size; do not read small gaps as real.")
    if s.get("scope_warning"):
        print(f"SCOPE: docs under {s.get('max_len')} chars only - not whole-corpus recall.")
    print(f"WROTE {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+",
                    help="incumbent | qwen2.5 | qwen3 | codex | sonnet | haiku; "
                         "suffix +verify to score the 3a+3b pipeline, e.g. qwen2.5+verify")
    ap.add_argument("--union", metavar="A,B",
                    help="also score the pooled union of these models, e.g. qwen2.5,qwen3")
    ap.add_argument("--sample", type=Path, default=SAMPLE)
    ap.add_argument("--labels", type=Path, default=LABELS)
    a = ap.parse_args()
    main(a.models, a.union, a.sample, a.labels)
