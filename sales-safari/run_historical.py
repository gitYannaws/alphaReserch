"""M1 CLI, historical mode: collect a subreddit's FULL history -> store documents.

  python run_historical.py https://www.reddit.com/r/ExpatFIRE/
  python run_historical.py <seed> --limit 50        # cap threads for a smoke run

Why this exists: run.py hardcodes the default corpus mode, which routes Reddit to
the live old.reddit crawl. That crawl is currently answered with a bot
interstitial (HTTP 200, zero posts), so it collects nothing. `corpus_mode
="historical"` routes to the Arctic Shift archive instead - the same switch the
web GUI's "historical" toggle flips - which is an API, makes zero reddit.com
requests, and carries exact created_utc + score plus the full history rather than
Reddit's ~1000-item listing cap.

Deliberately a sibling of run.py rather than a flag on it: run.py is the upstream
project's file and this box tracks that repo, so keeping the historical driver
separate leaves `git pull` conflict-free.
"""
import argparse
import sys
import time
import uuid

from dotenv import load_dotenv

from pipeline.orchestrate import load_config, normalize_seed, pick_collector
from pipeline.store import Store

load_dotenv()


# A DEADLINE, not an attempt count. "How long can the internet be down before this
# gives up?" is the question that actually matters, and an attempt count answers it
# only after you multiply the backoff series out - which is exactly how the first
# version of this shipped claiming ~16 min of cover while really giving 7m45s
# (15+30+60+120+240; the doubling series' last step is the give-up, so it never
# sleeps that long).
RETRY_DEADLINE_SECONDS = 45 * 60   # survive a 45-minute outage
RETRY_BASE_DELAY = 15
RETRY_MAX_DELAY = 300              # cap: past 5 min, waiting longer only delays
                                   # noticing that the link came back


def _collect_with_retry(collector, seed, limit, store, run_id,
                        deadline_seconds: int = RETRY_DEADLINE_SECONDS):
    """Collect, surviving a dropped connection for up to `deadline_seconds`.

    ArcticShiftCollector retries 429 internally but NOT connection errors - line
    ~151 turns any requests.RequestException straight into ArcticShiftError. On a
    laptop over home wifi that means a flicker kills a multi-hour backfill
    outright, which is the one thing a long scrape must not do.

    Restarting the generator re-walks the archive from the beginning, but
    upsert_document is an upsert: already-stored docs return falsey and are
    skipped, so a retry costs API pages, never duplicates. That is why it is safe
    to simply re-enter collect() rather than checkpoint a cursor.

    The delay doubles but caps at RETRY_MAX_DELAY, so a long outage is polled
    every 5 minutes rather than slept through in one ever-growing block - the run
    resumes within ~5 min of the link returning instead of up to an hour later.
    """
    new, seen_titles = 0, set()
    started = time.monotonic()
    delay = RETRY_BASE_DELAY
    attempt = 0
    while True:
        try:
            for doc in collector.collect(seed, limit):
                if store.upsert_document(run_id, doc):
                    new += 1
                    if doc.title not in seen_titles:
                        seen_titles.add(doc.title)
                        if len(seen_titles) % 25 == 0:
                            print(f"  ...{len(seen_titles)} threads, {new} docs", flush=True)
            return new, seen_titles
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            attempt += 1
            elapsed = time.monotonic() - started
            if elapsed + delay > deadline_seconds:
                print(f"  giving up after {elapsed / 60:.1f} min of failures "
                      f"({attempt} attempts): {exc.__class__.__name__}: {exc}", flush=True)
                return new, seen_titles
            print(f"  {exc.__class__.__name__}: {exc}", flush=True)
            print(f"  attempt {attempt}, retry in {delay}s "
                  f"({elapsed / 60:.1f}/{deadline_seconds / 60:.0f} min elapsed, "
                  f"{new} docs kept)", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_DELAY)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seed_url", nargs="?", help="subreddit or forum board URL")
    ap.add_argument("--limit", type=int, help="max threads (default: config max_threads)")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    seed = args.seed_url or (cfg.get("seed_urls") or [None])[0]
    if not seed:
        sys.exit("no seed_url (pass as arg or set config.seed_urls)")
    seed = normalize_seed(seed)

    limit = args.limit or cfg.get("collection", {}).get("max_threads", 100)
    collector, kind = pick_collector(seed, cfg, corpus_mode="historical")
    store = Store(cfg.get("db_path", "db/safari.sqlite"))

    run_id = uuid.uuid4().hex[:12]
    store.start_run(run_id, seed)
    print(f"run {run_id}  seed {seed}  limit {limit}  collector {kind}", flush=True)
    if kind != "arctic-shift":
        # Loud, because silently falling back to the live crawl is exactly the
        # failure this script exists to avoid - it would appear to work and collect 0.
        print(f"  WARNING: expected arctic-shift, got {kind!r}. "
              f"Check collection.arctic_shift.enabled in {args.config}.", flush=True)

    new, seen_titles = 0, set()
    try:
        new, seen_titles = _collect_with_retry(collector, seed, limit, store, run_id)
    except KeyboardInterrupt:
        # Partial history is still useful, and upserts are already committed - record
        # where it stopped instead of losing the run row to an exception.
        print("\ninterrupted - keeping what was collected", flush=True)

    store.set_stage(run_id, 2, "collected")
    print(f"done. {new} new docs across {len(seen_titles)} threads, "
          f"{store.count_distinct_authors(run_id)} distinct authors.", flush=True)
    store.close()


if __name__ == "__main__":
    main()
