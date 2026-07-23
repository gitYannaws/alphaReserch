"""Freeze one reproducible 50/50 benchmark sample.

known-pain docs  = docs with >=1 accepted pain row (existing verbatim spans attached)
random no-pain    = docs from EXTRACTED runs (runs that produced pains) that got 0 pains
                    -> genuine true-negatives, not merely unprocessed docs.

Seeded so every model reads the identical docs. Writes db/finish-bench-sample.json.
"""
import json, random, sqlite3, time, yaml
from pathlib import Path

SEED = 20260716
N_KNOWN = 50
N_RANDOM = 50
BATCH = 5
OUT = Path("db/finish-bench-sample.json")

cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
con = sqlite3.connect(cfg["db_path"]); con.row_factory = sqlite3.Row; cur = con.cursor()

known_ids = [r[0] for r in cur.execute("select distinct document_id from pains").fetchall()]
rand_ids = [r[0] for r in cur.execute(
    """select d.id from documents d
       where d.run_id in (select distinct run_id from pains)
         and d.id not in (select document_id from pains)
         and coalesce(d.raw_markdown,'')<>''""").fetchall()]

rng = random.Random(SEED)
known_pick = sorted(rng.sample(known_ids, N_KNOWN))
rand_pick = sorted(rng.sample(rand_ids, N_RANDOM))


def load_doc(doc_id, with_spans):
    d = cur.execute(
        "select id,title,raw_markdown from documents where id=?", (doc_id,)).fetchone()
    rec = {"id": d["id"], "title": d["title"] or "", "text": d["raw_markdown"]}
    if with_spans:
        rec["existing_spans"] = [
            r[0] for r in cur.execute(
                "select verbatim_span from pains where document_id=?", (doc_id,)).fetchall()
            if r[0]]
    return rec


sample = {
    "seed": SEED,
    "batch_size": BATCH,
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "pool_sizes": {"known_pain": len(known_ids), "true_negative": len(rand_ids)},
    "known_pain_docs": [load_doc(i, True) for i in known_pick],
    "random_no_pain_docs": [load_doc(i, False) for i in rand_pick],
}
con.close()
OUT.write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")

kd, rd = sample["known_pain_docs"], sample["random_no_pain_docs"]
avg = sum(len(x["text"]) for x in kd + rd) / (len(kd) + len(rd))
print(f"WROTE {OUT}  known={len(kd)} random={len(rd)} batch={BATCH} "
      f"avg_doc_chars={avg:.0f} seed={SEED}")
print("known ids:", [x["id"] for x in kd][:8], "...")
