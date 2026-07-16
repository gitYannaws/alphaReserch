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
_POST = re.compile(
    r'data-author="([^"]*)".*?data-content="post-(\d+)".*?<div class="bbWrapper">(.*?)</div>\s*</div>',
    re.S,
)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')


class XenforoCollectorError(RuntimeError):
    pass


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _clean(html: str) -> str:
    html = re.sub(r"<blockquote.*?</blockquote>", " ", html, flags=re.S)   # drop quoted text
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return unescape(_WS.sub(" ", re.sub(r"<[^>]+>", " ", html))).strip()


class XenforoCollector(Collector):
    source_type = "forum"

    def __init__(self, thread_patterns=None, same_domain_only: bool = True,
                 max_threads: int = 25, min_delay_seconds: float = 1.2,
                 max_delay_seconds: float = 2.6, timeout: int = 25,
                 max_thread_pages: int = 10, rl_backoff: float = 1.6,
                 rl_max_mult: float = 8.0, rl_recover: float = 0.9,
                 rl_retries: int = 2, rl_cooldown_seconds: float = 5.0):
        self.thread_patterns = thread_patterns or ["/threads/"]
        self.same_domain_only = same_domain_only
        self.max_threads = max_threads
        self.max_thread_pages = max_thread_pages
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
        t = r.text.lower()
        return any(m in t for m in ('data-xf-init', 'xf-init', 'id="xf"', 'xenforo'))

    def _get(self, url: str) -> str:
        for attempt in range(self.limiter.retries + 1):
            r = self.sess.get(url, timeout=self.timeout)
            if r.status_code in (403, 429):
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

    @staticmethod
    def _base_thread(url: str) -> str:
        # Collapse /threads/slug.123/page-2 , /post-9 , /unread ... to the base thread URL.
        m = re.match(r"^(.*?/threads/[^/]+)(?:/.*)?$", url)
        return m.group(1) if m else url

    def _thread_links(self, html: str, seed_url: str) -> List[str]:
        seed_host = _host(seed_url)
        out, seen = [], set()
        for href in re.findall(r'href="([^"]+)"', html):
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
            times = _TIME.findall(html)
            posts = _POST.findall(html)
            if not posts:
                break
            for i, (author, post_id, body) in enumerate(posts):
                text = _clean(body)
                if len(text) < 3:
                    continue
                yield Document(
                    source_type=self.source_type,
                    source_url=f"{base}#post-{post_id}",
                    permalink=f"{base}#post-{post_id}",
                    title=title,
                    raw_markdown=text,
                    source_granularity="post",
                    author=unescape(author) or None,
                    thread_url=base,
                    created_at=times[i] if i < len(times) else None,
                )
            # next page only if a pageNav "next" link exists
            if f"/page-{page + 1}" not in html:
                break
            page += 1
            self._sleep()

    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        seed = seed_url.split("#", 1)[0].rstrip("/")
        if self._is_thread(seed):
            threads = [seed]
        else:
            threads = self._thread_links(self._get(seed), seed)[: self.max_threads]
            if not threads:
                raise XenforoCollectorError(
                    f"no thread links matching {self.thread_patterns} on {seed}"
                )
        # `limit` = number of THREADS (matches other collectors + the "Max threads" UI).
        # Every post within a collected thread is yielded.
        for turl in threads[:limit]:
            self._sleep()
            try:
                for doc in self._thread_posts(turl):
                    yield doc
            except XenforoCollectorError as e:
                print(f"  skip {turl}: {e}")
                continue
