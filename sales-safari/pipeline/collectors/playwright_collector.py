"""Playwright collector for open, automation-permitted sites that need JS rendering.

This is a rendering fallback, not an evasion layer:
- no stealth plugins
- no proxy rotation
- no CAPTCHA solving
- slows/retries on 403/429, login walls, CAPTCHA, and bot-wall text, then stops
"""
import hashlib
import re
from typing import Iterator, List
from urllib.parse import urljoin, urlparse

from .base import Collector, Document
from .xenforo_collector import base_thread_url, is_xenforo_html, parse_thread_posts
from pipeline.domain_policy import find_unsupported_domain, format_unsupported_domain_error
from pipeline.rate_limit import AdaptiveRateLimiter

BLOCKED_TEXT = (
    "captcha",
    "access denied",
    "forbidden",
    "too many requests",
    "verify you are human",
    "enable javascript and cookies",
    "login required",
    "sign in to continue",
    # JS interstitials that stand in front of the real page. Safe to list only because a
    # match now costs a retry and, failing that, one skipped thread -- not a whole run.
    # Without them a challenge that has not resolved by wait_after_load_ms gets stored as
    # a ~130-char "Checking your browser" document.
    "checking your browser",
    "verifying your connection",
    "just a moment",
)

# These markers are ordinary English ("forbidden", "captcha"), so matching them anywhere
# on a page makes forum members trip the bot-wall detector by writing about them. A
# member of geomax.me mentioning "forbidden knowledge" aborted a whole crawl. A real wall
# replaces the page and says so up top, so only a SHORT page gets its PREFIX scanned.
BLOCK_MAX_BODY_CHARS = 1500     # a real thread runs 10k+ chars; a wall page is tiny
BLOCK_SCAN_CHARS = 600          # walls announce themselves before any content

# A site-wide block looks like every thread failing in a row; a single unreachable thread
# is just one bad page. Skip the latter, stop on the former.
MAX_CONSECUTIVE_THREAD_FAILURES = 5

_WS = re.compile(r"\s+")

# Site chrome that innerText would otherwise prepend to every single document
# ("Forums Members Rules What's new Log in Register Search New posts ..."), plus quoted
# text, which duplicates another post's words into this one's evidence.
BOILERPLATE_SELECTOR = (
    "script, style, noscript, nav, header, footer, aside, form, "
    "blockquote, [role=navigation], [role=banner], [role=contentinfo], [role=search], "
    "[aria-hidden=true]"
)

_STRIP_JS = """
(sel) => {
  const root = document.body ? document.body.cloneNode(true) : null;
  if (!root) return '';
  root.querySelectorAll(sel).forEach((el) => el.remove());
  return root.innerText || '';
}
"""

_CANONICAL_JS = """
() => {
  const l = document.querySelector('link[rel="canonical"]');
  return l ? l.href : '';
}
"""

# rel="next" is the standard forward-pagination signal and is what forum software emits
# in <head> for a thread listing (XenForo: <link rel="next" href="/forums/x.2/page-2">).
# Following it is how thread discovery reaches past the first listing page.
_NEXT_JS = """
() => {
  const el = document.querySelector('link[rel="next"], a[rel="next"]');
  return el ? el.href : '';
}
"""


class PlaywrightCollectorError(RuntimeError):
    pass


def _text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def is_blocked_text(text: str | None) -> bool:
    """Whether page text is a bot/login wall, as opposed to content that discusses one.

    Length gate first: a wall page replaces the content, so it is short. Anything with a
    real thread's worth of text is content, however many blocked-list words a member used.
    """
    text = text or ""
    if len(text) > BLOCK_MAX_BODY_CHARS:
        return False
    low = text[:BLOCK_SCAN_CHARS].lower()
    return any(marker in low for marker in BLOCKED_TEXT)


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
                 max_thread_pages: int = 10, max_listing_pages: int = 1,
                 post_level: bool = True, skip_thread_urls=None,
                 wait_after_load_ms: int = 1200, scroll_steps: int = 0,
                 scroll_delay_ms: int = 500, max_scrolls: int = 40,
                 scroll_settle_rounds: int = 2, rl_backoff: float = 1.6,
                 rl_max_mult: float = 8.0, rl_recover: float = 0.9,
                 rl_retries: int = 2, rl_cooldown_seconds: float = 5.0):
        self.thread_patterns = thread_patterns or ([thread_pattern] if thread_pattern else [])
        self.same_domain_only = same_domain_only
        self.max_pages = max_pages
        self.max_thread_pages = max_thread_pages
        # Listing pages of THREAD LINKS to walk before collecting. 1 = seed page only,
        # which is the historical behaviour. A big board is mostly unreachable at 1, but
        # raising it multiplies the whole run: threads-per-page x pages-per-thread fetches.
        self.max_listing_pages = max_listing_pages
        # Thread roots already held for this corpus. A deep backfill is measured in hours,
        # so a runner that dies partway must not re-walk what it already collected: the
        # resume runner re-enters stage 1 while progress is short of the cap, and these
        # let it pick up where it stopped. Left None on refresh runs, which need to
        # revisit known threads to capture posts added since.
        self.skip_thread_urls = {base_thread_url(_norm_url(u))
                                 for u in (skip_thread_urls or ())}
        # When the rendered DOM is recognizably post-structured, emit per-post documents
        # instead of one thread blob. Set False to force the old thread-level behaviour.
        self.post_level = post_level
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
        self.limiter = AdaptiveRateLimiter(
            min_delay_seconds, max_delay_seconds, backoff=rl_backoff,
            max_multiplier=rl_max_mult, recover=rl_recover,
            retries=rl_retries, cooldown_seconds=rl_cooldown_seconds,
        )

    @property
    def rate_limit_hits(self) -> int:
        return self.limiter.hits

    def _sleep(self):
        self.limiter.sleep()

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

    def _check_page(self, page, url: str) -> bool:
        """True when the page looks like a bot/login wall rather than content."""
        return is_blocked_text(
            page.evaluate("() => document.body ? document.body.innerText : ''"))

    def _goto(self, page, url: str):
        for attempt in range(self.limiter.retries + 1):
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            code = resp.status if resp else None
            if code in (403, 429):
                if self.limiter.hit(attempt):
                    continue
                raise PlaywrightCollectorError(
                    f"HTTP {code} for {url} after {attempt + 1} tries; stopping")
            page.wait_for_timeout(self.wait_after_load_ms)
            if self._check_page(page, url):
                if self.limiter.hit(attempt):
                    continue
                raise PlaywrightCollectorError(
                    f"blocked page detected for {url} after {attempt + 1} tries; stopping")
            self.limiter.success()
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

    def _canonical(self, page, url: str) -> str:
        """The page's own declared canonical URL, else the requested one.

        Web-standard, platform-agnostic dedupe key: XenForo serves the same thread page at
        /threads/x.9700, /threads/x.9700/latest and /threads/x.9700/page-2, and without this
        each alias lands in the corpus as its own document.
        """
        try:
            href = page.evaluate(_CANONICAL_JS) or ""
        except Exception:
            href = ""
        href = _norm_url(href) if href else ""
        if not href or (self.same_domain_only and _host(href) != _host(url)):
            return _norm_url(url)
        return href

    def _page_text(self, page, url: str) -> str:
        text = _WS.sub(" ", page.evaluate(_STRIP_JS, BOILERPLATE_SELECTOR) or "").strip()
        if not text:
            raise PlaywrightCollectorError(f"no readable text for {url}")
        return text

    def _document(self, page, url: str) -> Document:
        return Document(
            source_type=self.source_type,
            source_url=url,
            permalink=url,
            title=page.title() or url,
            raw_markdown=self._page_text(page, url),
            source_granularity="thread",
            thread_url=url,
        )

    def _xenforo_posts(self, page, url: str) -> List[Document]:
        """Post-level docs from a rendered XenForo thread, walking its pagination.

        Rendering is the only way onto boards that gate plain requests behind a JS
        challenge; once the page is in the DOM the static XenForo parser gives back the
        per-post authors that stage 5's distinct-author count depends on.
        """
        base = base_thread_url(url)
        title = (page.title() or "").split("|")[0].strip() or base
        docs, seen_posts = [], set()
        page_no = 1
        while page_no <= self.max_thread_pages:
            if page_no > 1:
                self._sleep()
                self._goto(page, f"{base}/page-{page_no}")
                self._scroll(page)
            html = page.content()
            found = parse_thread_posts(html, base, self.source_type, title)
            fresh = [d for d in found if d.permalink not in seen_posts]
            if not fresh:
                break
            for d in fresh:
                seen_posts.add(d.permalink)
                docs.append(d)
            if f"/page-{page_no + 1}" not in html:
                break
            page_no += 1
        return docs

    def _iter_listing_threads(self, page, seed_url: str, max_docs: int) -> Iterator[str]:
        """Thread roots from the listing, following rel="next" across listing pages.

        Reading only the seed page caps a run at whatever that one page links -- 21
        threads of the ~5300 on the board run e81aed813e0f was pointed at. Deliberately
        LAZY, one listing page at a time: collecting all 254 listings up front means
        ~an hour before the first document is stored, no progress signal for the GUI,
        and discovery thrown away entirely if the runner dies partway. The caller has
        already loaded and scrolled the seed page, and navigates `page` away between
        yields, so each later listing page is re-fetched by URL.
        """
        seen, skipped, yielded = set(), 0, 0
        listing = _norm_url(seed_url)
        for page_no in range(1, max(1, self.max_listing_pages) + 1):
            if page_no > 1:
                self._sleep()
                self._goto(page, listing)
                self._scroll(page)
            roots = []
            for url in self._links(page, listing):
                # Keep the thread root: a listing links the same thread several ways
                # (/latest, /post-N, /unread) and each alias would cost a fetch.
                root = base_thread_url(url)
                if root in seen:
                    continue
                seen.add(root)
                if root in self.skip_thread_urls:
                    skipped += 1
                    continue
                roots.append(root)
            nxt = page.evaluate(_NEXT_JS) or ""
            nxt = _norm_url(urljoin(listing, nxt)) if nxt else ""
            if self.same_domain_only and nxt and _host(nxt) != _host(seed_url):
                nxt = ""
            # Read the next-link BEFORE handing control back: the consumer navigates
            # `page` to each thread, so the listing DOM is gone by the time we resume.
            for root in roots:
                yield root
                yielded += 1
                if yielded >= max_docs:
                    return
            if skipped:
                print(f"  listing page {page_no}: skipped {skipped} threads already collected")
            if not nxt or nxt == listing:
                return
            listing = nxt

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
                    urls = iter([_norm_url(seed_url)])
                else:
                    self._goto(page, seed_url)
                    self._scroll(page)
                    urls = self._iter_listing_threads(page, seed_url, max_docs)

                saved = 0
                queued = 0
                seen_canonical, seen_text = set(), set()
                consecutive_failures = 0
                for url in urls:
                    queued += 1
                    self._sleep()
                    # Fetching is inside the try: a single thread that 403s, times out or
                    # renders a wall must not end a crawl of thousands. Only an unbroken
                    # run of failures is treated as the site shutting us out.
                    try:
                        self._goto(page, url)
                        self._scroll(page)
                        canonical = self._canonical(page, url)
                    except Exception as e:
                        consecutive_failures += 1
                        print(f"  skip {url}: {e}")
                        if consecutive_failures >= MAX_CONSECUTIVE_THREAD_FAILURES:
                            raise PlaywrightCollectorError(
                                f"{consecutive_failures} consecutive thread failures, "
                                f"last {url}: {e}") from e
                        continue
                    consecutive_failures = 0
                    # Both keys: the declared canonical, and the thread root it belongs to.
                    # /threads/x.9700/latest and /threads/x.9700/page-2 declare different
                    # canonicals but are the same thread, and the post walk below covers
                    # every page of it in one visit.
                    keys = {canonical, base_thread_url(canonical), base_thread_url(url)}
                    if keys & seen_canonical:
                        print(f"  skip {url}: duplicate of {canonical}")
                        continue
                    seen_canonical |= keys
                    try:
                        if self.post_level and is_xenforo_html(page.content()):
                            docs = self._xenforo_posts(page, url)
                        else:
                            docs = [self._document(page, canonical)]
                    except PlaywrightCollectorError as e:
                        print(f"  skip {url}: {e}")
                        continue
                    for doc in docs:
                        key = _text_key(doc.raw_markdown)
                        if key in seen_text:
                            continue
                        seen_text.add(key)
                        saved += 1
                        yield doc
                if queued == 0:
                    raise PlaywrightCollectorError(
                        "no thread links found on "
                        f"{seed_url} matching {self.thread_patterns}; "
                        "adjust collection.thread_url_patterns"
                    )
                if saved == 0:
                    raise PlaywrightCollectorError(
                        f"visited {queued} thread links but saved 0 readable pages"
                    )
            finally:
                browser.close()
