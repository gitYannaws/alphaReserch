"""XenForo collector: static HTML over plain requests with a real browser User-Agent.

XenForo boards serve full post HTML without JS, but sit behind
Cloudflare, which 403s non-browser User-Agents. A browser UA + Accept-Language from a
residential IP returns 200. This is deliberately NOT a headless browser: headless adds
bot signals (navigator.webdriver, HeadlessChrome) that Cloudflare also flags.

Operator owns the robots.txt / ToS decision for the seed domain.
"""
import re
from html import unescape
from typing import Iterator, List
from urllib.parse import urljoin, urlparse

import requests

from .base import Collector, Document
from pipeline.rate_limit import AdaptiveRateLimiter

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_WS = re.compile(r"\s+")
# Pair each post's author with its id. The negative lookahead stops the scan from running
# past the next post's data-author and mis-attributing the id.
_POST_HEAD = re.compile(
    r'data-author="([^"]*)"(?:(?!data-author=).)*?data-content="post-(\d+)"', re.S
)
_BBWRAPPER = re.compile(r'<div class="bbWrapper"[^>]*>')
_DIV_TAG = re.compile(r"</?div\b", re.I)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')
_XF_MARKERS = ('data-xf-init', 'xf-init', 'id="xf"', 'xenforo')
# XenForo emits <link rel="next" href="/forums/x/page-2"> in <head> for a paginated
# listing. Matching the head link (not the pageNav buttons) keeps this off the thread
# links, which carry their own /page-N aliases.
_NEXT = re.compile(r'<link[^>]+rel="next"[^>]+href="([^"]+)"', re.I)
# One row of the forum's own thread list (as opposed to a sidebar widget).
_STRUCT_ITEM_TITLE = re.compile(r'class="structItem-title"[^>]*>(.*?)</div>', re.S)
_HREF = re.compile(r'href="([^"]+)"')


class XenforoCollectorError(RuntimeError):
    pass


# A guest-gated or removed page, as opposed to a throttle. Both arrive as 403, but this
# one never clears by waiting. Length gate first, same reasoning as the renderer's
# is_blocked_text: a wall replaces the content, so it is short - a real thread that
# happens to discuss "not available" is not a wall.
_PERMANENT_BLOCK_MARKERS = (
    "content not available or blocked",
    "you must be logged in",
    "members only",
    "no permission to view",
    "do not have permission",
)


def is_permanent_block(html: str) -> bool:
    if not html or len(html) > 20000:
        return False
    low = re.sub(r"<[^>]+>", " ", html[:20000]).lower()
    return any(m in low for m in _PERMANENT_BLOCK_MARKERS)


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _strip_balanced(html: str, tag: str) -> str:
    """Remove every <tag>...</tag> region, honouring nesting.

    `<blockquote.*?</blockquote>` is non-greedy, so on a nested quote (A quotes B, who
    quoted C) it stops at the INNER close and leaves the outer quote's tail behind --
    which is how 46 posts ended up stored as "Click to expand... <the actual reply>".
    """
    pat = re.compile(rf"</?{tag}\b", re.I)
    out, depth, pos = [], 0, 0
    for m in pat.finditer(html):
        if m.group(0).startswith("</"):
            if depth > 0:
                depth -= 1
                if depth == 0:
                    end = html.find(">", m.end())
                    pos = end + 1 if end != -1 else m.end()
        else:
            if depth == 0:
                out.append(html[pos:m.start()])
            depth += 1
    out.append(html[pos:] if depth == 0 else "")
    return " ".join(out)


def _clean(html: str) -> str:
    html = _strip_balanced(html, "blockquote")        # drop quoted text, nesting and all
    html = _strip_balanced(html, "script")
    return unescape(_WS.sub(" ", re.sub(r"<[^>]+>", " ", html))).strip()


def _bbwrapper_body(html: str, from_index: int) -> str:
    """Inner HTML of the first bbWrapper at/after from_index, matching <div> depth.

    A plain non-greedy regex cannot do this: a quoted post nests several divs inside the
    wrapper, so `(.*?)</div></div>` terminates inside the quote block. That silently
    truncated every reply-with-quote to just the quote it was answering, losing the
    author's own words and leaving an unclosed <blockquote> that _clean could not strip.
    """
    m = _BBWRAPPER.search(html, from_index)
    if not m:
        return ""
    start = m.end()
    depth = 1
    for tag in _DIV_TAG.finditer(html, start):
        depth += -1 if tag.group(0).startswith("</") else 1
        if depth == 0:
            return html[start:tag.start()]
    return html[start:]          # unterminated markup: keep what we have


def base_thread_url(url: str) -> str:
    """Collapse /threads/slug.123/page-2 , /post-9 , /latest , /unread to the base thread URL."""
    m = re.match(r"^(.*?/threads/[^/]+)(?:/.*)?$", url)
    return m.group(1) if m else url


def is_xenforo_html(html: str) -> bool:
    """Marker test on already-fetched HTML, so a renderer can classify what it loaded."""
    low = (html or "").lower()
    return any(m in low for m in _XF_MARKERS)


def parse_thread_posts(html: str, base_url: str, source_type: str = "forum",
                       title: str | None = None) -> List[Document]:
    """Post-level Documents out of one XenForo thread page.

    Split out of the collector so any HTML source can reuse it -- notably the Playwright
    renderer, which is the only way onto boards that gate plain requests behind a JS
    challenge. Returns [] when the HTML holds no recognizable posts.
    """
    base = base_thread_url(base_url)
    if title is None:
        tm = _TITLE.search(html)
        title = unescape(tm.group(1).split("|")[0].strip()) if tm else base
    times = _TIME.findall(html)
    out: List[Document] = []
    for i, head in enumerate(_POST_HEAD.finditer(html)):
        author, post_id = head.group(1), head.group(2)
        text = _clean(_bbwrapper_body(html, head.end()))
        if len(text) < 3:
            continue
        out.append(Document(
            source_type=source_type,
            source_url=f"{base}#post-{post_id}",
            permalink=f"{base}#post-{post_id}",
            title=title,
            raw_markdown=text,
            source_granularity="post",
            author=unescape(author) or None,
            thread_url=base,
            created_at=times[i] if i < len(times) else None,
        ))
    return out


class XenforoCollector(Collector):
    source_type = "forum"

    def __init__(self, thread_patterns=None, same_domain_only: bool = True,
                 max_threads: int = 25, min_delay_seconds: float = 1.2,
                 max_delay_seconds: float = 2.6, timeout: int = 25,
                 max_thread_pages: int = 10, max_listing_pages: int = 1,
                 skip_thread_urls=None, rl_backoff: float = 1.6,
                 rl_max_mult: float = 8.0, rl_recover: float = 0.9,
                 rl_retries: int = 2, rl_cooldown_seconds: float = 5.0):
        self.thread_patterns = thread_patterns or ["/threads/"]
        self.same_domain_only = same_domain_only
        self.max_threads = max_threads
        self.max_thread_pages = max_thread_pages
        # Listing pages of THREAD LINKS to walk. 1 = seed page only, which was the
        # historical behaviour and strands a board: glp1forum.com/forums/public lists
        # ~78 threads per page of 2.5K, i.e. 3% of that one forum.
        self.max_listing_pages = max_listing_pages
        # Thread roots already held for this corpus, so a backfill that dies partway
        # resumes instead of re-walking what it collected (see the resumability rule).
        self.skip_thread_urls = {base_thread_url(u.split("#", 1)[0].rstrip("/"))
                                 for u in (skip_thread_urls or ())}
        self.min_delay = min_delay_seconds
        self.max_delay = max_delay_seconds
        self.timeout = timeout
        self.sess = requests.Session()
        self.sess.headers.update(UA)
        self.limiter = AdaptiveRateLimiter(
            min_delay_seconds, max_delay_seconds, backoff=rl_backoff,
            max_multiplier=rl_max_mult, recover=rl_recover,
            retries=rl_retries, cooldown_seconds=rl_cooldown_seconds,
        )

    @property
    def rate_limit_hits(self) -> int:
        return self.limiter.hits

    @staticmethod
    def is_xenforo(seed: str) -> bool:
        try:
            r = requests.get(seed, headers=UA, timeout=15)
        except requests.RequestException:
            return False
        if r.status_code != 200:
            return False
        return is_xenforo_html(r.text)

    def _get(self, url: str) -> str:
        for attempt in range(self.limiter.retries + 1):
            r = self.sess.get(url, timeout=self.timeout)
            if r.status_code in (403, 429):
                # A board can 403 one THREAD while serving its listings fine: glp1forum
                # gates some stickies behind login and returns its own short "Content Not
                # Available or Blocked" page. That is permanent for a guest, so retrying
                # it costs minutes and -- worse -- each retry bumps the adaptive
                # multiplier, slowing every later request on a board that is not actually
                # throttling us. Skip it now and leave the limiter alone.
                if is_permanent_block(r.text):
                    raise XenforoCollectorError(
                        f"HTTP {r.status_code} for {url}: members-only or blocked page; skipping")
                if self.limiter.hit(attempt):
                    continue
                raise XenforoCollectorError(
                    f"HTTP {r.status_code} for {url} after {attempt + 1} tries; skipping")
            r.raise_for_status()
            self.limiter.success()
            return r.text

    def _sleep(self):
        self.limiter.sleep()

    def _is_thread(self, url: str) -> bool:
        return any(p in url for p in self.thread_patterns)

    _base_thread = staticmethod(base_thread_url)

    def _thread_links(self, html: str, seed_url: str) -> List[str]:
        seed_host = _host(seed_url)
        out, seen = [], set()
        # A XenForo listing carries far more thread links than the board it shows: the
        # "latest posts" sidebar links threads from every other forum on the site. The
        # 1-thread /forums/dg/ board yielded 27 roots and 650 posts that belong to other
        # corpora. Each thread ROW is a .structItem-title, so read the rows when the page
        # has them and fall back to the whole page for themes that don't.
        rows = _STRUCT_ITEM_TITLE.findall(html)
        hrefs = ([m.group(1) for m in (_HREF.search(r) for r in rows) if m] if rows
                 else re.findall(r'href="([^"]+)"', html))
        for href in hrefs:
            url = urljoin(seed_url, href).split("#", 1)[0].rstrip("/")
            if not url.startswith(("http://", "https://")):
                continue
            if self.same_domain_only and _host(url) != seed_host:
                continue
            if not self._is_thread(url):
                continue
            url = self._base_thread(url)          # dedupe page-2/page-3 into one thread
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    def _thread_posts(self, thread_url: str) -> Iterator[Document]:
        """Yield every post in a thread, walking pagination up to max_thread_pages."""
        base = self._base_thread(thread_url)
        page = 1
        title = None
        while page <= self.max_thread_pages:
            url = base if page == 1 else f"{base}/page-{page}"
            html = self._get(url)
            if title is None:
                tm = _TITLE.search(html)
                title = unescape(tm.group(1).split("|")[0].strip()) if tm else base
            docs = parse_thread_posts(html, base, self.source_type, title)
            if not docs:
                break
            yield from docs
            # next page only if a pageNav "next" link exists
            if f"/page-{page + 1}" not in html:
                break
            page += 1
            self._sleep()

    def _iter_listing_threads(self, seed_url: str, max_threads: int) -> Iterator[str]:
        """Thread roots from the listing, following rel="next" across listing pages.

        Deliberately LAZY, one listing page at a time: walking every listing up front
        means no document is stored until discovery finishes, and a runner that dies
        partway throws the whole walk away.
        """
        seen, skipped, yielded = set(), 0, 0
        listing = seed_url
        for page_no in range(1, max(1, self.max_listing_pages) + 1):
            html = self._get(listing)
            roots = []
            for url in self._thread_links(html, listing):
                if url in seen:
                    continue
                seen.add(url)
                if url in self.skip_thread_urls:
                    skipped += 1
                    continue
                roots.append(url)
            if page_no == 1 and not roots and not skipped:
                raise XenforoCollectorError(
                    f"no thread links matching {self.thread_patterns} on {listing}"
                )
            for root in roots:
                yield root
                yielded += 1
                if yielded >= max_threads:
                    return
            if skipped:
                print(f"  listing page {page_no}: skipped {skipped} threads already collected")
            nxt = _NEXT.search(html)
            nxt = urljoin(listing, unescape(nxt.group(1))).split("#", 1)[0].rstrip("/") if nxt else ""
            if self.same_domain_only and nxt and _host(nxt) != _host(seed_url):
                nxt = ""
            if not nxt or nxt == listing:
                return
            listing = nxt
            self._sleep()

    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        seed = seed_url.split("#", 1)[0].rstrip("/")
        # `limit` = number of THREADS (matches other collectors + the "Max threads" UI).
        # Every post within a collected thread is yielded.
        cap = min(limit, self.max_threads)
        if self._is_thread(seed):
            threads = iter([seed])
        else:
            threads = self._iter_listing_threads(seed, cap)
        for turl in threads:
            self._sleep()
            try:
                for doc in self._thread_posts(turl):
                    yield doc
            except XenforoCollectorError as e:
                print(f"  skip {turl}: {e}")
                continue
