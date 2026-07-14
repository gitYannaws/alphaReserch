"""M1 CLI: collect a forum seed -> store documents.

  python run.py <seed_url> [--limit N]
  python run.py            # uses config.seed_urls[0]
"""
import sys
import uuid
import argparse
from dotenv import load_dotenv

from pipeline.orchestrate import load_config, normalize_seed, pick_collector
from pipeline.store import Store

load_dotenv()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seed_url", nargs="?", help="forum board or thread URL")
    ap.add_argument("--limit", type=int, help="max threads")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    seed = args.seed_url or (cfg.get("seed_urls") or [None])[0]
    if not seed:
        sys.exit("no seed_url (pass as arg or set config.seed_urls)")
    seed = normalize_seed(seed)

    limit = args.limit or cfg.get("collection", {}).get("max_threads", 100)
    collector, kind = pick_collector(seed, cfg)
    store = Store(cfg.get("db_path", "db/safari.sqlite"))

    run_id = uuid.uuid4().hex[:12]
    store.start_run(run_id, seed)
    print(f"run {run_id}  seed {seed}  limit {limit}  collector {kind}")

    new, seen_titles = 0, set()
    for doc in collector.collect(seed, limit):
        if store.upsert_document(run_id, doc):
            new += 1
            if doc.title not in seen_titles:
                seen_titles.add(doc.title)
                print(f"  thread: {doc.title[:70]}")

    store.set_stage(run_id, 2, "collected")
    print(f"done. {new} new docs across {len(seen_titles)} threads, "
          f"{store.count_distinct_authors(run_id)} distinct authors.")
    store.close()


if __name__ == "__main__":
    main()
