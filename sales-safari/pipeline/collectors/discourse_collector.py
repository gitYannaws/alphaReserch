"""Discourse collector via public JSON endpoints (plain requests, no scraping tools).

Discourse forums expose:
  {category}.json?page=N   -> topic_list.topics[]
  /t/{topic_id}.json       -> post_stream.posts[] (username, cooked, created_at)
                              + post_stream.stream[] (ids of EVERY post in the topic)
  /t/{topic_id}/posts.json?post_ids[]=... -> those posts, batched

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


def _likes(post: dict) -> Optional[int]:
    """Net endorsement for a post = the `actions_summary` entry with id 2 (Like).

    Deliberately NOT post["score"]: that is Discourse's internal popularity heuristic
    folding in reads/replies (observed values like 283.6), not a vote count. Returns
    None when the topic exposes no like summary, so scoring degrades to 0 rather than
    reading a fabricated number.
    """
    for a in post.get("actions_summary") or []:
        if a.get("id") == 2:
            try:
                return int(a.get("count") or 0)
            except (TypeError, ValueError):
                return None
    return None


class DiscourseCollector(Collector):
    source_type = "forum"

    def __init__(self, keywords: Optional[List[str]] = None, max_pages: int = 3,
                 min_post_len: int = 25, pause: float = 0.3, timeout: int = 30,
                 max_posts_per_thread: int = 200, post_batch: int = 100,
                 known_thread_stats: Optional[dict] = None):
        self.keywords = [k.lower() for k in (keywords or [])]
        self.max_pages = max_pages
        self.min_post_len = min_post_len
        self.pause = pause
        self.timeout = timeout
        self.max_posts_per_thread = max(1, int(max_posts_per_thread or 200))
        self.post_batch = max(1, int(post_batch or 100))
        # Delta refresh: {thread_url: {count, max_created_at}} of what the corpus already
        # holds. Keyed here by TOPIC ID (parsed from the stored thread_url) because the
        # topic list gives us ids, and a topic's slug can be edited while its id cannot.
        # A slug-only mismatch would just re-fetch one topic - safe, store dedupes by URL.
        self.known_topic_stats = {}
        for url, st in (known_thread_stats or {}).items():
            m = re.search(r"/t/(?:[^/]+/)?(\d+)", url or "")
            if m:
                self.known_topic_stats[int(m.group(1))] = st

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

    def _topic_is_unchanged(self, t: dict) -> bool:
        """True when re-fetching this topic can yield nothing new, judged from the topic
        LIST alone (zero extra requests).

        Two conditions, either suffices:
        - cap reached: we hold >= max_posts_per_thread docs. `stream[:cap]` takes the
          topic's OLDEST ids, and new posts append at the end, so a capped topic can never
          produce more docs no matter how much it grows.
        - nothing new upstream: the list's last_posted_at is not newer than the newest
          post we stored. Compared on the first 19 chars (second precision) of the ISO
          timestamps - both come from the same Discourse instance, so formats match.

        NOT compared: posts_count vs our count. min_post_len filtering means we always
        hold fewer docs than upstream counts posts, which would defeat the skip entirely.
        Cost of the timestamp route: a topic whose only new post got length-filtered
        re-fetches once per refresh until someone posts something longer. Acceptable.
        """
        st = self.known_topic_stats.get(t.get("id"))
        if not st:
            return False
        if int(st.get("count") or 0) >= self.max_posts_per_thread:
            return True
        last = str(t.get("last_posted_at") or "")[:19]
        have = str(st.get("max_created_at") or "")[:19]
        return bool(last and have and last <= have)

    def _keep_topic(self, title: str) -> bool:
        if not self.keywords:
            return True
        t = (title or "").lower()
        # word-boundary match so "cut" doesn't match "cute" / "material" not "materialism".
        return any(re.search(rf"\b{re.escape(k)}\b", t) for k in self.keywords)

    def _post_doc(self, p: dict, title: str, thread_url: str) -> Optional[Document]:
        text = _html_to_text(p.get("cooked", ""))
        if len(text) < self.min_post_len:
            return None
        n = p.get("post_number", 1)
        return Document(
            source_type=self.source_type,
            source_url=f"{thread_url}/{n}",
            permalink=f"{thread_url}/{n}",
            title=title,
            raw_markdown=text,
            source_granularity="post",
            author=p.get("username"),
            thread_url=thread_url,
            created_at=p.get("created_at"),
            score=_likes(p),
        )

    def _thread_posts(self, base: str, topic_id: int):
        """Yield every post in a topic, up to `max_posts_per_thread`.

        /t/{id}.json embeds only the FIRST ~20 posts in post_stream.posts, but lists the
        ids of all of them in post_stream.stream. Reading posts[] alone silently truncates
        every topic at 20 - and long reply chains are exactly where unresolved complaints
        live. Remaining ids are fetched from /t/{id}/posts.json?post_ids[]=... .
        """
        j = self._get(f"{base}/t/{topic_id}.json")
        title = j.get("title", "")
        slug = j.get("slug", str(topic_id))
        thread_url = f"{base}/t/{slug}/{topic_id}"
        ps = j.get("post_stream") or {}
        first = ps.get("posts") or []
        # Cap on ids fetched (not docs yielded) so API cost per topic stays predictable
        # even on a 2000-post megathread.
        wanted = (ps.get("stream") or [])[: self.max_posts_per_thread]

        for p in first[: self.max_posts_per_thread]:
            doc = self._post_doc(p, title, thread_url)
            if doc:
                yield doc

        have = {p.get("id") for p in first}
        rest = [i for i in wanted if i not in have]
        for k in range(0, len(rest), self.post_batch):
            chunk = rest[k:k + self.post_batch]
            qs = "&".join(f"post_ids[]={i}" for i in chunk)
            try:
                pj = self._get(f"{base}/t/{topic_id}/posts.json?{qs}")
            except (requests.HTTPError, ValueError) as e:
                # Keep the posts we already have rather than losing the whole topic.
                print(f"  topic {topic_id}: post batch failed ({e}); kept {len(first)} posts")
                return
            for p in (pj.get("post_stream") or {}).get("posts") or []:
                doc = self._post_doc(p, title, thread_url)
                if doc:
                    yield doc
            time.sleep(self.pause)

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
            topics = topics[:limit]
            if self.known_topic_stats:
                fresh = [t for t in topics if not self._topic_is_unchanged(t)]
                if len(fresh) < len(topics):
                    print(f"  delta refresh: skipped {len(topics) - len(fresh)} unchanged "
                          f"topics of {len(topics)}")
                topics = fresh
            topic_ids = [t["id"] for t in topics]
        for tid in topic_ids:
            try:
                yield from self._thread_posts(base, tid)
            except requests.HTTPError as e:
                print(f"  skip topic {tid}: {e}")
                continue
            time.sleep(self.pause)
