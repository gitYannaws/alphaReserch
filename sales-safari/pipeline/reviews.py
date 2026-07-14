"""Stage 9.6: mine 1-2 star app-store reviews of the discovered competitors.

Low-star reviews of an incumbent are concentrated "the incumbent is bad here"
evidence - the exact signal your thesis wants (complaints that outlive an
incumbent's launch = opportunity). This uses ONLY free, public Apple endpoints:

  - iTunes Search API   -> resolve a competitor name to an app id
  - iTunes customer-reviews RSS (JSON) -> that app's reviews, filtered to <= 2 stars

No key, no scraping gray zone. Products with no iOS app are skipped silently.
Advisory - never drops, never blocks the run.
"""
import re
import time

import requests

SEARCH_URL = "https://itunes.apple.com/search"
RSS_URL = "https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
UA = {"User-Agent": "sales-safari/1.0 (market research; contact via forum)"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_app(name: str, country: str, timeout: int = 10) -> dict | None:
    """Resolve a product name to an App Store app via the public Search API."""
    try:
        resp = requests.get(SEARCH_URL, params={
            "term": name, "entity": "software", "country": country, "limit": 5,
        }, headers=UA, timeout=timeout)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception:
        return None
    q = _norm(name)
    for r in results:
        track = _norm(r.get("trackName", ""))
        # Match on the app's OWN name only. Seller-name matching wrongly grabs unrelated
        # apps by the same publisher (e.g. "Humble Bundle" -> a game by Humble Games).
        if q and (q in track or track in q):
            return {"app_id": str(r.get("trackId")), "app_name": r.get("trackName"),
                    "url": r.get("trackViewUrl", "")}
    return None


def _reviews_from_feed(feed: dict) -> list:
    entries = feed.get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for e in entries:
        rating = (e.get("im:rating") or {}).get("label")
        if rating is None:  # the app-metadata entry, not a review
            continue
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            continue
        out.append({
            "review_id": (e.get("id") or {}).get("label"),
            "rating": rating,
            "title": (e.get("title") or {}).get("label", ""),
            "body": (e.get("content") or {}).get("label", ""),
            "author": ((e.get("author") or {}).get("name") or {}).get("label", ""),
            "version": (e.get("im:version") or {}).get("label", ""),
        })
    return out


def fetch_low_star(app_id: str, country: str, max_pages: int = 3, max_stars: int = 2,
                   timeout: int = 10) -> list:
    reviews = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(
                RSS_URL.format(country=country, page=page, app_id=app_id),
                headers=UA, timeout=timeout)
            if resp.status_code != 200:
                break
            feed = resp.json().get("feed", {})
        except Exception:
            break
        page_reviews = _reviews_from_feed(feed)
        if not page_reviews:
            break
        reviews.extend(r for r in page_reviews if r["rating"] <= max_stars)
        time.sleep(0.2)  # be polite to Apple's endpoint
    return reviews


def reviews_run(store, run_id: str, countries=None, max_pages: int = 3,
                max_stars: int = 2, max_per_competitor: int = 25,
                progress=None) -> dict:
    countries = countries or ["us"]
    competitors = store.get_competitors(run_id)
    store.clear_reviews(run_id)
    matched, saved = 0, 0
    if progress:
        progress(0, len(competitors))
    for i, c in enumerate(competitors, start=1):
        app = None
        for country in countries:
            app = find_app(c["name"], country)
            if app:
                break
        if not app:
            if progress:
                progress(i, len(competitors))
            continue
        matched += 1
        kept = 0
        for country in countries:
            if kept >= max_per_competitor:
                break
            for r in fetch_low_star(app["app_id"], country, max_pages, max_stars):
                r.update(app_id=app["app_id"], app_name=app["app_name"], country=country,
                         source_url=app["url"])
                if store.save_review(run_id, c["id"], r):
                    saved += 1
                    kept += 1
                if kept >= max_per_competitor:
                    break
        if progress:
            progress(i, len(competitors))
    return {"competitors": len(competitors), "matched": matched, "reviews": saved}


if __name__ == "__main__":
    import argparse
    from pipeline.orchestrate import load_config
    from pipeline.store import Store
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--countries", default="us")
    args = ap.parse_args()
    cfg = load_config()
    store = Store(cfg.get("db_path", "db/safari.sqlite"))
    rcfg = cfg.get("reviews", {})
    print(reviews_run(store, args.run_id,
                      countries=args.countries.split(","),
                      max_pages=rcfg.get("max_pages", 3),
                      max_stars=rcfg.get("max_stars", 2),
                      max_per_competitor=rcfg.get("max_per_competitor", 25)))
    store.close()
