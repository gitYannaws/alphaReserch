"""Discourse collector via public JSON endpoints (plain requests, no scraping tools).

Discourse forums expose:
  {category}.json?page=N   -> topic_list.topics[]
  /t/{topic_id}.json       -> post_stream.posts[] (username, cooked, created_at)

Structured + free. One post = one Document, author attached.
"""
import re
import time
import requests
from typing import Iterator, List, Optional
from urllib.parse import urlparse

from .base import Collector, Document

UA = {"User-Agent": "sales-safari research (contact: local)"}
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", html or "")).strip()


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


class DiscourseCollector(Collector):
    source_type = "forum"

    def __init__(self, keywords: Optional[List[str]] = None, max_pages: int = 3,
                 min_post_len: int = 25, pause: float = 0.3, timeout: int = 30):
        self.keywords = [k.lower() for k in (keywords or [])]
        self.max_pages = max_pages
        self.min_post_len = min_post_len
        self.pause = pause
        self.timeout = timeout

    # ---- detection ----
    @staticmethod
    def is_discourse(url: str) -> bool:
        try:
            r = requests.get(url.rstrip("/") + ".json", headers=UA, timeout=15)
            if r.status_code != 200:
                return False
            j = r.json()
            return "topic_list" in j or "post_stream" in j
        except Exception:
            return False

    def _get(self, url: str) -> dict:
        r = requests.get(url, headers=UA, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ---- enumeration ----
    def _category_topics(self, cat_url: str) -> List[dict]:
        topics = []
        for page in range(self.max_pages):
            try:
                j = self._get(f"{cat_url.rstrip('/')}.json?page={page}")
            except requests.HTTPError:
                break
            page_topics = (j.get("topic_list") or {}).get("topics") or []
            if not page_topics:
                break
            topics.extend(page_topics)
            time.sleep(self.pause)
        return topics

    def _keep_topic(self, title: str) -> bool:
        if not self.keywords:
            return True
        t = (title or "").lower()
        # word-boundary match so "cut" doesn't match "cute" / "material" not "materialism".
        return any(re.search(rf"\b{re.escape(k)}\b", t) for k in self.keywords)

    def _thread_posts(self, base: str, topic_id: int):
        j = self._get(f"{base}/t/{topic_id}.json")
        title = j.get("title", "")
        slug = j.get("slug", str(topic_id))
        thread_url = f"{base}/t/{slug}/{topic_id}"
        for p in (j.get("post_stream") or {}).get("posts") or []:
            text = _html_to_text(p.get("cooked", ""))
            if len(text) < self.min_post_len:
                continue
            n = p.get("post_number", 1)
            yield Document(
                source_type=self.source_type,
                source_url=f"{thread_url}/{n}",
                permalink=f"{thread_url}/{n}",
                title=title,
                raw_markdown=text,
                source_granularity="post",
                author=p.get("username"),
                thread_url=thread_url,
                created_at=p.get("created_at"),
            )

    # ---- main ----
    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        base = _origin(seed_url)
        if "/t/" in seed_url:
            m = re.search(r"/t/(?:[^/]+/)?(\d+)", seed_url)
            topic_ids = [int(m.group(1))] if m else []
            titles = {tid: "" for tid in topic_ids}
        else:
            topics = self._category_topics(seed_url)
            topics = [t for t in topics if self._keep_topic(t.get("title", ""))]
            topic_ids = [t["id"] for t in topics[:limit]]
        for tid in topic_ids:
            try:
                yield from self._thread_posts(base, tid)
            except requests.HTTPError as e:
                print(f"  skip topic {tid}: {e}")
                continue
            time.sleep(self.pause)
