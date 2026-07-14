"""Playwright collector for open, automation-permitted sites that need JS rendering.

This is a rendering fallback, not an evasion layer:
- no stealth plugins
- no proxy rotation
- no CAPTCHA solving
- stops on 403/429, login walls, CAPTCHA, and bot-wall text
"""
import random
import re
import time
from typing import Iterator, List
from urllib.parse import urljoin, urlparse

from .base import Collector, Document
from pipeline.domain_policy import find_unsupported_domain, format_unsupported_domain_error

BLOCKED_TEXT = (
    "captcha",
    "access denied",
    "forbidden",
    "too many requests",
    "verify you are human",
    "enable javascript and cookies",
    "login required",
    "sign in to continue",
)

_WS = re.compile(r"\s+")


class PlaywrightCollectorError(RuntimeError):
    pass


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _norm_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


class PlaywrightCollector(Collector):
    source_type = "forum"
    granularity = "thread"        # whole-thread text, no per-post author

    def __init__(self, thread_pattern: str = "/t/", same_domain_only: bool = True,
                 thread_patterns=None, max_pages: int = 25, min_delay_seconds: float = 3.0,
                 max_delay_seconds: float = 8.0, timeout_ms: int = 30000,
                 headless: bool = True, allowed_domains=None, unsupported_domains=None,
                 wait_after_load_ms: int = 1200, scroll_steps: int = 0,
                 scroll_delay_ms: int = 500, max_scrolls: int = 40,
                 scroll_settle_rounds: int = 2):
        self.thread_patterns = thread_patterns or ([thread_pattern] if thread_pattern else [])
        self.same_domain_only = same_domain_only
        self.max_pages = max_pages
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.allowed_domains = [d.lower() for d in (allowed_domains or [])]
        self.unsupported_domains = unsupported_domains or []
        self.wait_after_load_ms = wait_after_load_ms
        self.scroll_steps = scroll_steps
        self.scroll_delay_ms = scroll_delay_ms
        # Infinite ("eternal") scroll: keep scrolling a lazy-loading feed until the
        # page stops growing. max_scrolls is a hard safety cap; scroll_settle_rounds
        # is how many consecutive no-growth rounds prove the feed is exhausted.
        self.max_scrolls = max_scrolls
        self.scroll_settle_rounds = scroll_settle_rounds

    def _sleep(self):
        time.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))

    def _check_allowed(self, seed_url: str):
        host = _host(seed_url)
        unsupported = find_unsupported_domain(host, self.unsupported_domains)
        if unsupported:
            entry, domain = unsupported
            raise PlaywrightCollectorError(
                format_unsupported_domain_error(host, entry, domain, "Playwright fallback")
            )
        if self.allowed_domains and not any(host == d or host.endswith(f".{d}") for d in self.allowed_domains):
            raise PlaywrightCollectorError(
                f"Playwright fallback is not allowed for {host}; add it to collection.playwright.allowed_domains."
            )

    def _check_page(self, page, url: str):
        status = page.evaluate("() => document.body ? document.body.innerText : ''")
        low = (status or "").lower()
        if any(marker in low for marker in BLOCKED_TEXT):
            raise PlaywrightCollectorError(f"blocked page detected for {url}; stopping")

    def _goto(self, page, url: str):
        resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        code = resp.status if resp else None
        if code in (403, 429):
            raise PlaywrightCollectorError(f"HTTP {code} for {url}; stopping")
        page.wait_for_timeout(self.wait_after_load_ms)
        self._check_page(page, url)
        return resp

    def _scroll(self, page):
        """Drain a lazy-loading (infinite) feed.

        scroll_steps == 0 disables scrolling (static pages). When enabled it is the
        MINIMUM number of rounds to scroll before a height plateau is allowed to stop
        us — this keeps a slow feed from being cut off by an early no-growth blip.
        We scroll to the bottom, wait for new content, and stop once scrollHeight has
        not grown for scroll_settle_rounds consecutive rounds, or the max_scrolls cap.
        """
        if self.scroll_steps <= 0:
            return
        cap = max(self.max_scrolls, self.scroll_steps)
        last_height = page.evaluate("() => document.body ? document.body.scrollHeight : 0")
        stable = 0
        for i in range(cap):
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(self.scroll_delay_ms)
            height = page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            if height > last_height:
                stable = 0
            else:
                stable += 1
                # honor the configured minimum before trusting a plateau
                if i + 1 >= self.scroll_steps and stable >= self.scroll_settle_rounds:
                    break
            last_height = height

    def _links(self, page, seed_url: str) -> List[str]:
        seed_host = _host(seed_url)
        raw = page.eval_on_selector_all(
            "a[href]",
            "(els) => els.map((a) => a.getAttribute('href')).filter(Boolean)",
        )
        out, seen = [], set()
        for href in raw:
            url = _norm_url(urljoin(seed_url, href))
            if not url.startswith(("http://", "https://")):
                continue
            if self.same_domain_only and _host(url) != seed_host:
                continue
            if self.thread_patterns and not any(p in url for p in self.thread_patterns):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    def _document(self, page, url: str) -> Document:
        title = page.title() or url
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        text = _WS.sub(" ", text).strip()
        if not text:
            raise PlaywrightCollectorError(f"no readable text for {url}")
        return Document(
            source_type=self.source_type,
            source_url=url,
            permalink=url,
            title=title,
            raw_markdown=text,
            source_granularity="thread",
            thread_url=url,
        )

    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        self._check_allowed(seed_url)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise PlaywrightCollectorError(
                "Playwright is not installed. Run: .venv/Scripts/python -m pip install playwright "
                "and then .venv/Scripts/python -m playwright install chromium"
            ) from e

        max_docs = min(limit, self.max_pages)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            try:
                context = browser.new_context(
                    user_agent="sales-safari research; contact local operator",
                    viewport={"width": 1280, "height": 900},
                    java_script_enabled=True,
                )
                page = context.new_page()
                if self.thread_patterns and any(p in seed_url for p in self.thread_patterns):
                    urls = [_norm_url(seed_url)]
                else:
                    self._goto(page, seed_url)
                    self._scroll(page)
                    urls = self._links(page, seed_url)[:max_docs]
                    if not urls:
                        raise PlaywrightCollectorError(
                            "no thread links found on "
                            f"{seed_url} matching {self.thread_patterns}; adjust collection.thread_url_patterns"
                        )

                saved = 0
                for url in urls[:max_docs]:
                    self._sleep()
                    self._goto(page, url)
                    self._scroll(page)
                    try:
                        doc = self._document(page, url)
                    except PlaywrightCollectorError as e:
                        print(f"  skip {url}: {e}")
                        continue
                    saved += 1
                    yield doc
                if saved == 0:
                    raise PlaywrightCollectorError(
                        f"found {len(urls[:max_docs])} thread links but saved 0 readable pages"
                    )
            finally:
                browser.close()
