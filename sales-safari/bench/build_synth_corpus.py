"""Generate a synthetic corpus with known planted signal, ready for a real analyze run.

    .venv/Scripts/python bench/build_synth_corpus.py                 # clean-room corpus
    .venv/Scripts/python bench/build_synth_corpus.py --hybrid 3000   # + real docs as noise
    .venv/Scripts/python bench/build_synth_corpus.py --noise-ratio 6 --seed 7

Writes documents under corpus_key `synth:v1` (override with --key) and the ground truth to
`db/synth-truth.json`. Then analyze it exactly like any other corpus - the pipeline is not
told which documents are planted:

    open the web GUI -> Sources -> select `synth:v1` -> Analyze
    then: .venv/Scripts/python bench/score_synth.py <job_id>

Every planted document carries its theme in the source_url path (`synth://T01/...`), so
scoring maps document -> theme deterministically. Nothing downstream reads that path.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.synth_spec import NOISE_TEMPLATES, THEMES, THEMES_ROUND2  # noqa: E402
from pipeline.collectors.base import Document  # noqa: E402
from pipeline.store import Store  # noqa: E402

DB = "db/safari.sqlite"
TRUTH = Path("db/synth-truth.json")


def _iso(base: datetime, days: float) -> str:
    return (base - timedelta(days=days)).isoformat()


def build(key: str, seed: int, noise_ratio: float, hybrid: int,
          hybrid_from: str = "reddit:r/thepassportbros", round2: bool = False) -> dict:
    rnd = random.Random(seed)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    store = Store(DB)
    store.ensure_corpus(key, f"synth://{key}")

    # A synthetic corpus is only meaningful against ONE spec, so rebuild from clean.
    conn: sqlite3.Connection = store.conn
    old = [r[0] for r in conn.execute(
        "SELECT document_id FROM corpus_documents WHERE corpus_key=?", (key,))]
    if old:
        marks = ",".join("?" * len(old))
        conn.execute(f"DELETE FROM documents WHERE id IN ({marks})", old)
        conn.execute("DELETE FROM corpus_documents WHERE corpus_key=?", (key,))
        conn.commit()
        print(f"cleared {len(old):,} documents from a previous build of {key}")

    truth = {"corpus_key": key, "seed": seed, "noise_ratio": noise_ratio,
             "hybrid_real_docs": hybrid, "built_at": datetime.now(timezone.utc).isoformat(),
             "themes": [], "planted_doc_ids": {}, "noise_source_urls": []}
    planted = 0

    themes = THEMES + (THEMES_ROUND2 if round2 else ())
    for t in themes:
        authors = [f"synth_{t.id}_a{i:02d}" for i in range(t.n_authors)]
        threads = [f"synth://{t.id}/thread/{i}" for i in range(t.n_threads)]
        urls = []
        # Author count is the independent variable the filters threshold on, so it must
        # land EXACTLY on the spec. Generating one post per paraphrase silently capped
        # distinct authors at len(complaints) - T01 asked for 9 and got 6. Emit
        # max(paraphrases, n_authors) posts instead, cycling the paraphrase list, and give
        # each of the first n_authors posts its own author.
        n_posts = max(len(t.complaints), t.n_authors)
        for i in range(n_posts):
            complaint = t.complaints[i % len(t.complaints)]
            author = authors[i % len(authors)]
            thread = threads[i % len(threads)]
            url = f"synth://{t.id}/post/{i}"
            urls.append(url)
            store.upsert_document("synth-build", Document(
                source_type="synth", source_url=url, permalink=url,
                title=f"{t.persona} thread {i % t.n_threads + 1}",
                raw_markdown=complaint, source_granularity="post",
                author=author, thread_url=thread,
                created_at=_iso(base, rnd.uniform(1, 120)), score=rnd.randint(0, 12)))
            store.link_document_to_corpus(key, store.get_document_id_by_source_url(url))
            planted += 1
        truth["themes"].append({
            "id": t.id, "label": t.label, "persona": t.persona,
            "saas_shaped": t.saas_shaped, "expect_survives": t.expect_survives,
            "n_authors": t.n_authors, "n_threads": t.n_threads,
            "n_posts": max(len(t.complaints), t.n_authors), "note": t.note, "post_urls": urls})

    # Distractors, tagged by class so precision can be reported per failure mode.
    n_noise = int(planted * noise_ratio)
    classes = list(NOISE_TEMPLATES)
    for i in range(n_noise):
        cls = classes[i % len(classes)]
        body = rnd.choice(NOISE_TEMPLATES[cls])
        url = f"synth://noise/{cls}/{i}"
        store.upsert_document("synth-build", Document(
            source_type="synth", source_url=url, permalink=url,
            title=f"general chat {i // 7 + 1}", raw_markdown=body,
            source_granularity="post", author=f"synth_noise_a{i % 40:02d}",
            thread_url=f"synth://noise/thread/{i // 7}",
            created_at=_iso(base, rnd.uniform(1, 120)), score=rnd.randint(0, 9)))
        store.link_document_to_corpus(key, store.get_document_id_by_source_url(url))
        truth["noise_source_urls"].append(url)

    # Hybrid mode: real forum text as background, so the realism gap is measured not assumed.
    #
    # Default background is r/thepassportbros, NOT geomax.me. Measured 2026-08-16:
    #   r/thepassportbros  123,863 docs / 22,045 authors /   5.6 docs per author
    #   geomax.me           64,822 docs /    496 authors / 130.7 docs per author
    # geomax is a small clique posting constantly. A background with 496 authors cannot
    # exercise the distinct-author filters at all - run 329746779047 dropped 1,538 clusters
    # for insufficient_authors against exactly that background. Benchmarking the filters
    # needs a background whose author diversity resembles the corpora you actually mine.
    real_linked = 0
    if hybrid:
        avail = conn.execute(
            "SELECT COUNT(*) FROM corpus_documents WHERE corpus_key=?", (hybrid_from,)).fetchone()[0]
        if not avail:
            keys = [r[0] for r in conn.execute(
                "SELECT corpus_key FROM corpus_documents GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 8")]
            raise SystemExit(f"--hybrid-from {hybrid_from!r} has no documents.\n"
                             "  largest available:\n    " + "\n    ".join(keys))
        # Sample across the whole corpus rather than the first N rows: LIMIT alone would
        # take one contiguous slice, which on a forum means a handful of threads.
        rows = conn.execute(
            "SELECT d.id FROM corpus_documents cd JOIN documents d ON d.id=cd.document_id "
            "WHERE cd.corpus_key=? AND LENGTH(COALESCE(d.raw_markdown,'')) > 80 "
            "ORDER BY d.id LIMIT ?", (hybrid_from, hybrid)).fetchall()
        for (doc_id,) in rows:
            if store.link_document_to_corpus(key, doc_id):
                real_linked += 1
        conn.commit()
    truth["hybrid_real_linked"] = real_linked
    truth["hybrid_from"] = hybrid_from if hybrid else None

    conn.commit()
    store.close()
    TRUTH.write_text(json.dumps(truth, indent=1), encoding="utf-8")

    print(f"\ncorpus_key      {key}")
    print(f"planted docs    {planted:,}   across {len(themes)} themes")
    print(f"distractors     {n_noise:,}   ({noise_ratio:.1f}x planted, {len(classes)} classes)")
    print(f"real docs mixed {real_linked:,}" + (f"   from {hybrid_from}" if real_linked else ""))
    print(f"total           {planted + n_noise + real_linked:,}")
    print(f"\nexpected to survive: {sum(1 for t in themes if t.expect_survives)}"
          f" of {len(themes)} themes")
    print(f"truth written   {TRUTH}")
    print("\nnext: analyze this corpus in the GUI, then")
    print("      .venv/Scripts/python bench/score_synth.py <job_id>")
    return truth


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default="synth:v1")
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--noise-ratio", type=float, default=4.0,
                    help="distractor docs per planted doc (default 4)")
    ap.add_argument("--hybrid", type=int, default=0,
                    help="also link N real documents as background noise")
    ap.add_argument("--hybrid-from", default="reddit:r/thepassportbros",
                    help="corpus_key to draw that background from "
                         "(default r/thepassportbros: 22,045 authors vs geomax.me's 496)")
    ap.add_argument("--round2", action="store_true",
                    help="add the round-2 hard themes (resolved-past-tense, two adjacent "
                         "themes, sarcasm) -- round 1 saturated at 100%% recall")
    a = ap.parse_args()
    build(a.key, a.seed, a.noise_ratio, a.hybrid, a.hybrid_from, a.round2)
