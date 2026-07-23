"""Freeze a BLIND, length-stratified sample for human gold labelling.

WHY NOT build_sample.py
-----------------------
build_sample.py draws its labels from the `pains` table - a doc is "known pain" only
because a previous extractor flagged it. Scoring a model against that measures
agreement with the old extractor, not recall. This sampler never reads `pains`.

DESIGN
------
- Population = docs that a run actually EXTRACTED (reached stage 3+) AND under MAX_LEN
  chars. 66% of the corpus has never been through extraction; including it would put docs
  in the denominator that no extractor ever saw.
- Stratified by raw_markdown LENGTH - an attribute independent of any extractor's opinion,
  which correlates strongly with pain density (3.4% -> 16.5% across the buckets below).
  A pure random sample at the 10.9% base rate wastes most of the labelling budget on
  short chit-chat docs.

SCOPE LIMIT - READ THIS BEFORE QUOTING ANY NUMBER FROM IT
--------------------------------------------------------
MAX_LEN caps doc length to keep labelling tractable. At 500 chars the sample covers 88.5%
of extracted docs but only ~69% of pain-docs: long posts are 11.5% of the corpus and hold
31% of the pain. So scores from this sample are recall **on docs under MAX_LEN chars**, not
overall recall, and they are blind to the densest end of the corpus (500+ chars runs 28-36%
pain vs 3.4% for the short tail). Raise MAX_LEN to 10**9 and re-freeze for a whole-corpus
figure. score_gold.py reweights within this population only - the weights cannot recover a
stratum that was never sampled.
- Equal N per bucket + recorded population weights => reweight at scoring time to recover
  unbiased population-level estimates (verified: reweighted estimate reproduces the true
  observed rate to 0.1pt).
- BLIND: no existing spans, no pain flags, no bucket label in the labelling payload, and
  docs are shuffled so buckets do not clump. The labeller must not be anchored by what the
  old extractor thought.

Writes db/gold-sample.json. Label it with bench/label.html, score with bench/score_gold.py.
"""
import argparse
import json
import random
import sqlite3
import time
from pathlib import Path

import yaml

SEED = 20260716
# 40 not 24: dropping the two long strata costs ~60% of the sample's pain-docs, so the
# per-bucket count rises to hold the recall denominator up. Short docs label fast enough
# that 120 of these is less work than the 120 mixed-length docs they replace.
PER_BUCKET = 40
MAX_LEN = 500          # see SCOPE LIMIT in the module docstring before changing
OUT = Path("db/gold-sample.json")
TEMPLATE = Path("bench/label.html")
PAGE = Path("bench/label-gold.html")


def emit_html(sample: dict, template: Path, page: Path):
    """Write a standalone labelling page with the sample inlined.

    Inlining (vs the file picker) means the page works from file:// with no fetch and no
    picking the right JSON. The data is embedded in a <script>, so every '<' is escaped to
    \\u003c - a post whose text contains '</script>' would otherwise close the tag early and
    break the page. JSON has no '<' outside string literals, so escaping all of them is safe
    and JS decodes \\u003c back to '<'.
    """
    html = template.read_text(encoding="utf-8")
    marker = "const INLINE_SAMPLE = null; /*__SAMPLE__*/"
    if marker not in html:
        raise SystemExit(f"marker not found in {template}; did label.html change?")
    blob = json.dumps(sample, ensure_ascii=False).replace("<", "\\u003c")
    page.write_text(html.replace(marker, f"const INLINE_SAMPLE = {blob};"), encoding="utf-8")
    return page

# (name, min_len, max_len) - max exclusive. Must tile [0, MAX_LEN) with no gap or overlap.
BUCKETS = [
    ("<100", 0, 100),
    ("100-199", 100, 200),
    ("200-499", 200, 500),
]

# Docs a run actually put through extraction, within the MAX_LEN cap. Deliberately does
# NOT mention `pains` - the labels must not come from the thing being measured.
POP = f"""
  FROM documents d
 WHERE COALESCE(d.raw_markdown,'') <> ''
   AND LENGTH(d.raw_markdown) < {MAX_LEN}
   AND EXISTS (SELECT 1 FROM run_documents rd JOIN runs r ON r.job_id = rd.run_id
                WHERE rd.document_id = d.id AND r.stage >= 3)
"""


def check_buckets():
    """BUCKETS must tile [0, MAX_LEN) exactly.

    A gap or a short last bucket would silently exclude docs that POP still counts in
    `total`, so the stratum weights would no longer sum to 1 and every reweighted estimate
    would be quietly wrong. Cheap to assert, near-impossible to notice otherwise.
    """
    edges = [(lo, hi) for _, lo, hi in BUCKETS]
    if edges[0][0] != 0:
        raise SystemExit(f"BUCKETS must start at 0, starts at {edges[0][0]}")
    if edges[-1][1] != MAX_LEN:
        raise SystemExit(
            f"BUCKETS end at {edges[-1][1]} but MAX_LEN is {MAX_LEN}; docs in "
            f"[{edges[-1][1]}, {MAX_LEN}) would be counted in the population but never "
            f"sampled, silently breaking the weights. Fix BUCKETS to tile [0, MAX_LEN)."
        )
    for (_, hi), (lo, _) in zip(edges, edges[1:]):
        if hi != lo:
            raise SystemExit(f"BUCKETS gap/overlap at {hi} -> {lo}")


def main(seed: int, per_bucket: int, out: Path):
    check_buckets()
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    con = sqlite3.connect(cfg["db_path"])
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    total = cur.execute("SELECT COUNT(*) " + POP).fetchone()[0]
    rng = random.Random(seed)

    docs, strata = [], []
    for name, lo, hi in BUCKETS:
        where = f" AND LENGTH(d.raw_markdown) >= {lo} AND LENGTH(d.raw_markdown) < {hi}"
        pool = [r[0] for r in cur.execute("SELECT d.id " + POP + where).fetchall()]
        if len(pool) < per_bucket:
            raise SystemExit(f"bucket {name}: only {len(pool)} docs, need {per_bucket}")
        picked = rng.sample(pool, per_bucket)
        strata.append({
            "bucket": name,
            "population": len(pool),
            "population_share": round(len(pool) / total, 6),
            "sampled": per_bucket,
            # Each sampled doc stands for this many population docs. Equal N per bucket
            # means every doc is sampled at rate 1/len(BUCKETS) within its stratum, so the
            # correction is just share / (1/len(BUCKETS)). score_gold.py multiplies by this
            # to undo the deliberate oversampling of the longer, pain-denser buckets.
            "weight": round((len(pool) / total) * len(BUCKETS), 6),
        })
        for did in picked:
            d = cur.execute(
                "SELECT id, title, raw_markdown FROM documents WHERE id=?", (did,)).fetchone()
            docs.append({
                "id": d["id"],
                "bucket": name,
                "title": d["title"] or "",
                "text": d["raw_markdown"],
            })
    con.close()

    # Shuffle so buckets do not clump; a labeller who sees 24 long docs in a row starts
    # pattern-matching on length instead of reading.
    rng.shuffle(docs)

    sample = {
        "seed": seed,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "population": total,
        "population_desc": f"extracted docs (run reached stage 3+) under {MAX_LEN} chars",
        "max_len": MAX_LEN,
        "scope_warning": (
            f"Scores from this sample are recall on docs under {MAX_LEN} chars, NOT overall "
            f"recall. Docs >= {MAX_LEN} chars are ~11.5% of extracted docs but hold ~31% of "
            f"pain-docs, and are excluded here. Do not quote these numbers as whole-corpus."
        ),
        "per_bucket": per_bucket,
        "strata": strata,
        "docs": docs,
        "label_schema": {
            "has_pain": "true|false - does this post state a genuine pain "
                        "(complaint, workflow friction, costly workaround, explicit wish)?",
            "spans": "list of EXACT substrings of text evidencing each distinct pain; "
                     "[] when has_pain is false",
            "note": "optional free text, e.g. why it was a close call",
        },
    }
    out.write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"WROTE {out}")
    print(f"  population {total} extracted docs | {len(docs)} docs to label | seed {seed}")
    print(f"  {'bucket':<9} {'pop':>7} {'share':>7} {'weight':>7}")
    for s in strata:
        print(f"  {s['bucket']:<9} {s['population']:>7} {s['population_share']*100:>6.1f}% {s['weight']:>7.3f}")
    print("\n  BLIND: no pain flags or existing spans included.")
    return sample


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--per-bucket", type=int, default=PER_BUCKET)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--emit-html", action="store_true",
                    help="also write bench/label-gold.html with the sample inlined")
    a = ap.parse_args()
    s = main(a.seed, a.per_bucket, a.out)
    if a.emit_html:
        p = emit_html(s, TEMPLATE, PAGE)
        print(f"\n  WROTE {p} ({p.stat().st_size/1024:.0f} KB) - open it and start labelling.")
        print("  Export saves gold-labels.json; move it to db/gold-labels.json.")
    else:
        print("  Next: open bench/label.html, load this file, label, export db/gold-labels.json")
