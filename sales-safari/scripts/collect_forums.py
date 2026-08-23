"""Collect a list of board URLs on ONE domain, strictly sequentially.

A board like glp1forum.com is 60+ sibling forums under one host, and the corpus key is
per seed path -- so each forum is its own corpus and its own run. The webapp's kickoff
does this in-process (dies with the server) and lanes are per-DOMAIN, so all siblings
share one lane anyway: this is that lane, detached and restartable.

Each run is created with use_corpus=1, which makes `pipeline.resume` stop after stage 1
(collect + store + corpus link) instead of running the LLM stages. Extraction is a
separate pass, run once the GPU is free.

  .venv/Scripts/python scripts/collect_forums.py urls.txt
  .venv/Scripts/python scripts/collect_forums.py urls.txt --resume   # skip done corpora
"""
import argparse
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from pipeline.orchestrate import corpus_key_for_seed, load_config, normalize_seed
from pipeline.resume import Runner
from pipeline.store import Store


def collect_one(seed: str, cfg: dict, db_path: str) -> int:
    job_id = uuid.uuid4().hex[:12]
    corpus_key = corpus_key_for_seed(seed)
    store = Store(db_path)
    try:
        # use_render maps to collection.fallback="render" in the resume runner: XenForo
        # over plain requests when the board is XenForo (one probe GET), Playwright only
        # if it is not. Leaving it off maps to fallback="none", which refuses every seed
        # that is not open Discourse JSON.
        store.start_run(job_id, seed, use_render=True, use_firecrawl=False,
                        use_corpus=True, extractor=None)
        store.ensure_corpus(corpus_key, seed)
        store.add_source(uuid.uuid4().hex[:12], seed, None, corpus_key)
    finally:
        store.close()
    logfile = str(ROOT / "db" / f"resume-{job_id}.log")
    runner = Runner(job_id, cfg, db_path, attempts=3, base_delay=5.0, logfile=logfile)
    try:
        print(f"[{seed}] job {job_id}", flush=True)
        return runner.run(1)
    finally:
        runner.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url_file", help="file of board URLs, one per line (# comments ok)")
    ap.add_argument("--resume", action="store_true",
                    help="skip boards whose corpus finished a backfill pass")
    args = ap.parse_args()

    load_dotenv()
    cfg = load_config(str(ROOT / "config.yaml"))
    db_path = str(ROOT / cfg.get("db_path", "db/safari.sqlite"))

    seeds = []
    for line in Path(args.url_file).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            seeds.append(normalize_seed(line))

    store = Store(db_path)
    try:
        # "Has documents" is the wrong resume test: a corpus interrupted at listing page
        # 3 of 40 has documents and is nowhere near done. backfill_completed_at is only
        # set once a collect pass ran to the end of the listing.
        done = {k for k in (corpus_key_for_seed(s) for s in seeds)
                if (store.get_corpus(k) or {}).get("backfill_completed_at")} if args.resume else set()
    finally:
        store.close()

    failed = []
    for i, seed in enumerate(seeds, 1):
        if corpus_key_for_seed(seed) in done:
            print(f"({i}/{len(seeds)}) skip {seed} (backfill already completed)", flush=True)
            continue
        print(f"({i}/{len(seeds)}) {seed}", flush=True)
        try:
            if collect_one(seed, cfg, db_path) != 0:
                failed.append(seed)
        except KeyboardInterrupt:
            print("interrupted", flush=True)
            break
        except Exception as e:
            print(f"  FAILED {seed}: {e}", flush=True)
            failed.append(seed)

    print(f"done. {len(seeds) - len(failed)} ok, {len(failed)} failed", flush=True)
    for s in failed:
        print(f"  failed: {s}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
