"""Firecrawl collector: map a forum board -> thread URLs -> scrape each to markdown.

Only points at OPEN forums (no login/bot wall). Not for Reddit-post-lockout evasion.
"""
import os
import re
import time
import requests
from typing import Iterator, List
from urllib.parse import urljoin, urlparse

from .base import Collector, Document

FIRECRAWL_BASE = "https://api.firecrawl.dev/v2"
UA = {"User-Agent": "sales-safari research; contact local operator"}
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.I)
BLOCKED_TEXT = (
    "content not available or blocked",
    "blocked by cloudflare",
    "verify you are human",
    "captcha",
    "access denied",
    "too many requests",
)


class FirecrawlError(RuntimeError):
    pass


class FirecrawlCollector(Collector):
    source_type = "forum"
    granularity = "thread"        # whole-thread markdown, no per-post author

    def __init__(self, api_key: str = None, thread_pattern: str = "/t/",
                 thread_patterns=None, same_domain_only: bool = True, timeout: int = 60,
                 pause: float = 0.5):
        self.api_key = api_key or os.environ["FIRECRAWL_API_KEY"]
        self.thread_patterns = thread_patterns or ([thread_pattern] if thread_pattern else [])
        self.same_domain_only = same_domain_only
        self.timeout = timeout
        self.pause = pause

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _raise_for_status(self, r: requests.Response, action: str, url: str):
        if r.status_code < 400:
            return
        body = (r.text or "").strip().replace("\n", " ")[:500]
        hint = ""
        if r.status_code == 401:
            hint = " Check FIRECRAWL_API_KEY."
        elif r.status_code == 403:
            hint = " Firecrawl forbids this target or account for this action; try a Discourse JSON forum or another open forum."
        raise FirecrawlError(f"Firecrawl {action} failed for {url}: HTTP {r.status_code}. {body}{hint}")

    def map(self, seed_url: str) -> List[str]:
        r = requests.post(f"{FIRECRAWL_BASE}/map", headers=self._headers(),
                          json={"url": seed_url}, timeout=self.timeout)
        self._raise_for_status(r, "map", seed_url)
        links = r.json().get("links", []) or []
        out = []
        for l in links:
            u = l if isinstance(l, str) else (l or {}).get("url")
            if u:
                out.append(u)
        return out

    def scrape(self, url: str):
        r = requests.post(f"{FIRECRAWL_BASE}/scrape", headers=self._headers(),
                          json={"url": url, "formats": ["markdown"]}, timeout=self.timeout)
        self._raise_for_status(r, "scrape", url)
        d = r.json().get("data", {}) or {}
        meta = d.get("metadata", {}) or {}
        return d.get("markdown", "") or "", meta.get("title", "") or ""

    def _is_blocked_content(self, text: str, title: str = "") -> bool:
        low = f"{title}\n{text}".lower()
        return any(marker in low for marker in BLOCKED_TEXT)

    def _thread_urls(self, seed_url: str) -> List[str]:
        seed_host = urlparse(seed_url).netloc
        seen, threads = set(), []
        candidates = self.map(seed_url)
        for u in candidates:
            if u in seen:
                continue
            if self.thread_patterns and not any(p in u for p in self.thread_patterns):
                continue
            if self.same_domain_only and urlparse(u).netloc != seed_host:
                continue
            seen.add(u)
            threads.append(u)
        if not threads:
            for u in self._html_thread_urls(seed_url):
                if u in seen:
                    continue
                seen.add(u)
                threads.append(u)
        return threads

    def _html_thread_urls(self, seed_url: str) -> List[str]:
        r = requests.get(seed_url, headers=UA, timeout=self.timeout)
        r.raise_for_status()
        seed_host = urlparse(seed_url).netloc
        out = []
        for href in _HREF.findall(r.text or ""):
            u = urljoin(seed_url, href).split("#", 1)[0].rstrip("/")
            if not u.startswith(("http://", "https://")):
                continue
            if self.same_domain_only and urlparse(u).netloc != seed_host:
                continue
            if self.thread_patterns and not any(p in u for p in self.thread_patterns):
                continue
            if u.endswith(("/latest", "/unread")):
                continue
            out.append(u)
        return out

    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        # A thread URL scrapes directly; a board URL is mapped first.
        if self.thread_patterns and any(p in seed_url for p in self.thread_patterns):
            urls = [seed_url]
        else:
            urls = self._thread_urls(seed_url)[:limit]
        for u in urls:
            try:
                md, title = self.scrape(u)
            except (requests.HTTPError, FirecrawlError) as e:
                print(f"  skip {u}: {e}")
                continue
            if not md.strip():
                continue
            if self._is_blocked_content(md, title):
                print(f"  skip {u}: blocked content page")
                continue
            yield Document(source_type=self.source_type, source_url=u,
                           permalink=u, title=title or u, raw_markdown=md,
                           source_granularity="thread")
            time.sleep(self.pause)
