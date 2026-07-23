"""Shared orchestration: config load + collector routing. Used by CLI and web app."""
from urllib.parse import urlparse

import yaml

from pipeline.collectors.discourse_collector import DiscourseCollector
from pipeline.collectors.fallback_collector import FallbackCollector
from pipeline.collectors.firecrawl_collector import FirecrawlCollector
from pipeline.collectors.playwright_collector import PlaywrightCollector
from pipeline.collectors.reddit_collector import RedditCollector, _seed_listing_prefs
from pipeline.collectors.xenforo_collector import XenforoCollector
from pipeline.domain_policy import find_unsupported_domain, format_unsupported_domain_error


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_seed(seed: str) -> str:
    """Accept scheme-less seeds like 'www.xyz.com/forum'. Default to https://.

    urlparse only fills netloc when a scheme is present, so a bare host lands in
    the path and every host check downstream sees an empty netloc.
    """
    seed = (seed or "").strip()
    if not seed:
        return seed
    if "://" not in seed:
        seed = "https://" + seed.lstrip("/")
    return seed


def corpus_key_for_seed(seed: str) -> str | None:
    """Stable corpus identifier for seeds that should share a reusable cache."""
    seed = normalize_seed(seed)
    if not seed:
        return None
    if RedditCollector.is_reddit(seed):
        path = urlparse(seed).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() == "r":
            return f"reddit:r/{parts[1].lower()}"
    p = urlparse(seed)
    path = (p.path or "/").rstrip("/") or "/"
    return f"web:{p.netloc.lower()}{path.lower()}"


def _normalize_reddit_sort_plan(items, default_time_filter: str) -> list[tuple[str, str | None]]:
    plan = []
    for raw in (items or []):
        if isinstance(raw, (list, tuple)) and raw:
            sort = str(raw[0] or "hot").lower()
            tf = raw[1] if len(raw) > 1 else None
        else:
            raw = str(raw or "hot").lower()
            sort, _, tf = raw.partition(":")
            tf = tf or None
        if sort == "best":
            sort = "hot"
        if sort in ("top", "controversial"):
            tf = tf or default_time_filter
        else:
            tf = None
        item = (sort or "hot", tf)
        if item not in plan:
            plan.append(item)
    return plan


def pick_collector(seed: str, cfg: dict, known_thread_urls=None, corpus_mode: str = "",
                   progress_cb=None, extra_thread_urls=None, known_thread_stats=None):
    """Discourse if detected; else configured open-forum fallback."""
    coll = cfg.get("collection", {})
    seed = normalize_seed(seed)
    host = urlparse(seed).netloc.lower()
    unsupported = find_unsupported_domain(host, coll.get("unsupported_domains", []))
    if unsupported:
        entry, domain = unsupported
        raise ValueError(format_unsupported_domain_error(host, entry, domain, "collection"))
    # Reddit is a per-comment source with its own free public JSON; route by host before
    # the generic Discourse probe (which would 404 on reddit and fall through to scraping).
    if RedditCollector.is_reddit(seed):
        # HISTORICAL runs swap the slow polite crawl for the Arctic Shift archive when
        # enabled: full subreddit history, exact timestamps + scores, zero reddit.com
        # requests. Refresh/backfill runs keep the live crawl (the archive trails the
        # fresh edge). Opt-in via collection.arctic_shift.enabled.
        azs = coll.get("arctic_shift", {})
        if azs.get("enabled") and corpus_mode == "historical":
            from pipeline.collectors.arcticshift_collector import ArcticShiftCollector
            return ArcticShiftCollector(
                api_base=azs.get("api_base") or None,
                page_size=azs.get("page_size", 100),
                pause=azs.get("pause", 0.7),
                min_comment_len=coll.get("reddit", {}).get("min_comment_len", 20),
                max_comments=azs.get("max_comments", 0),
                dump_dir=azs.get("dump_dir", ""),
                progress_cb=progress_cb,
            ), "arctic-shift"
        rc = coll.get("reddit", {})
        seed_sort, seed_time_filter = _seed_listing_prefs(seed)
        sort = seed_sort or rc.get("sort", "hot")
        stale_listing_pages = rc.get("stale_listing_pages", 3)
        max_listing_pages = rc.get("max_listing_pages", 36 if known_thread_urls else 12)
        cooldown_every_seconds = 60 * float(rc.get("cooldown_every_minutes", 0) or 0)
        cooldown_seconds = 60 * float(rc.get("cooldown_minutes", 0) or 0)
        time_filter = seed_time_filter or rc.get("time_filter", "year")
        skip_thread_urls = known_thread_urls
        sort_plan = [(sort, time_filter)]
        if corpus_mode in {"backfill", "historical"}:
            sort = rc.get("backfill_sort", "new")
            stale_listing_pages = rc.get("backfill_stale_listing_pages", 1000)
            max_listing_pages = rc.get("backfill_max_listing_pages", 250)
            cooldown_every_seconds = 60 * float(rc.get("backfill_cooldown_every_minutes", 0) or 0)
            cooldown_seconds = 60 * float(rc.get("backfill_cooldown_minutes", 0) or 0)
            time_filter = rc.get("backfill_time_filter", time_filter)
            backfill_cycle = rc.get(
                "backfill_sort_cycle",
                ["new", "top:all", "top:year", "controversial:all", "hot"],
            )
            sort_plan = _normalize_reddit_sort_plan([sort, *backfill_cycle], time_filter)
        elif known_thread_urls:
            sort = seed_sort or rc.get("corpus_sort", "new")
            # Refresh runs need to revisit the newest threads so upsert_document() can
            # capture newly-added comments on already-known submissions. Skipping known
            # thread URLs is still correct for historical backfills, but it makes refresh
            # miss fresh posts inside existing threads.
            skip_thread_urls = None
            refresh_cycle = rc.get("refresh_sort_cycle", ["new", "hot", "top"])
            sort_plan = _normalize_reddit_sort_plan([sort, *refresh_cycle], time_filter)
        return RedditCollector(
            keywords=cfg.get("keywords"),
            sort=sort,
            time_filter=time_filter,
            max_comments_per_thread=rc.get("max_comments_per_thread", 200),
            comment_depth=rc.get("comment_depth", 8),
            min_comment_len=rc.get("min_comment_len", 20),
            min_delay_seconds=rc.get("min_delay_seconds", 1.2),
            max_delay_seconds=rc.get("max_delay_seconds", 2.5),
            skip_thread_urls=skip_thread_urls,
            stale_listing_pages=stale_listing_pages,
            max_listing_pages=max_listing_pages,
            extra_thread_urls=extra_thread_urls,
            sort_plan=sort_plan,
            cooldown_every_seconds=cooldown_every_seconds,
            cooldown_seconds=cooldown_seconds,
            progress_cb=progress_cb,
        ), "reddit"

    if DiscourseCollector.is_discourse(seed):
        dc = coll.get("discourse", {})
        return DiscourseCollector(
            keywords=cfg.get("keywords"),
            max_pages=coll.get("max_pages", 3),
            min_post_len=dc.get("min_post_len", 25),
            max_posts_per_thread=dc.get("max_posts_per_thread", 200),
            post_batch=dc.get("post_batch", 100),
            # Delta refresh: with corpus stats the collector skips topics whose upstream
            # last-post time hasn't moved past what we already hold.
            known_thread_stats=known_thread_stats,
        ), "discourse"

    def firecrawl():
        return FirecrawlCollector(
            thread_pattern=coll.get("thread_url_pattern", "/t/"),
            thread_patterns=coll.get("thread_url_patterns"),
            same_domain_only=coll.get("same_domain_only", True),
        )

    def playwright():
        pw = coll.get("playwright", {})
        rl = coll.get("rate_limit", {})
        return PlaywrightCollector(
            thread_pattern=coll.get("thread_url_pattern", "/t/"),
            thread_patterns=coll.get("thread_url_patterns"),
            same_domain_only=coll.get("same_domain_only", True),
            max_pages=coll.get("max_threads", 25),
            min_delay_seconds=pw.get("min_delay_seconds", 3),
            max_delay_seconds=pw.get("max_delay_seconds", 8),
            timeout_ms=pw.get("timeout_ms", 30000),
            headless=pw.get("headless", True),
            allowed_domains=pw.get("allowed_domains", []),
            unsupported_domains=coll.get("unsupported_domains", []),
            wait_after_load_ms=pw.get("wait_after_load_ms", 1200),
            scroll_steps=pw.get("scroll_steps", 0),
            scroll_delay_ms=pw.get("scroll_delay_ms", 500),
            max_scrolls=pw.get("max_scrolls", 40),
            scroll_settle_rounds=pw.get("scroll_settle_rounds", 2),
            rl_backoff=rl.get("backoff", 1.6),
            rl_max_mult=rl.get("max_multiplier", 8.0),
            rl_recover=rl.get("recover", 0.9),
            rl_retries=rl.get("retries", 2),
            rl_cooldown_seconds=rl.get("cooldown_seconds", 5.0),
        )

    def xenforo():
        pw = coll.get("playwright", {})
        rl = coll.get("rate_limit", {})
        return XenforoCollector(
            thread_patterns=coll.get("thread_url_patterns") or ["/threads/"],
            same_domain_only=coll.get("same_domain_only", True),
            max_threads=coll.get("max_threads", 25),
            min_delay_seconds=pw.get("min_delay_seconds", 1.2),
            max_delay_seconds=pw.get("max_delay_seconds", 2.6),
            max_thread_pages=coll.get("max_thread_pages", 10),
            rl_backoff=rl.get("backoff", 1.6),
            rl_max_mult=rl.get("max_multiplier", 8.0),
            rl_recover=rl.get("recover", 0.9),
            rl_retries=rl.get("retries", 2),
            rl_cooldown_seconds=rl.get("cooldown_seconds", 5.0),
        )

    fallback = coll.get("fallback", "firecrawl")
    if fallback == "xenforo":
        return xenforo(), "xenforo"
    # "render" = auto-pick the open-forum renderer: static XenForo when the board is
    # XenForo (one probe GET), else headless Playwright. No Firecrawl, no manual choice.
    if fallback == "render":
        if XenforoCollector.is_xenforo(seed):
            return xenforo(), "xenforo"
        return playwright(), "playwright"
    if fallback == "auto" and XenforoCollector.is_xenforo(seed):
        return xenforo(), "xenforo"
    if fallback == "playwright":
        return playwright(), "playwright"
    if fallback == "auto":
        return FallbackCollector(firecrawl(), playwright(), "firecrawl", "playwright"), "auto"
    if fallback == "none":
        raise ValueError(
            "This seed is not an open Discourse JSON forum. Approve a collector in the website: "
            "enable Playwright rendering, Firecrawl, or both."
        )

    return firecrawl(), "firecrawl"
