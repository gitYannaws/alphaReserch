"""Topic -> candidate communities (pre-stage-1 discovery).

Turns a topic/category string into a reviewed list of seed URLs the rest of the
pipeline can run. Two steps:

  1. firecrawl_search  - Firecrawl web search for communities matching the topic.
  2. validate_candidate - probe each candidate and judge whether REAL people post
     there, so blogs / SEO / product pages get filtered out before a full run.

The legitimacy signal is the project's core rule: distinct authors, not post count.
Authored collectors (Reddit/Discourse/XenForo) expose per-post authors, so we count
distinct authors in a small free sample. Generic thread-level sources (Firecrawl)
yield authorless thread blobs, so we can only prove community *structure* cheaply
(map + thread-pattern filter, no scrape) and flag them as weak/unverified.

Firecrawl is opt-in and costs credits; discovery only runs when it is approved.
"""
import os
import re
from dataclasses import asdict, dataclass
from typing import List, Optional
from urllib.parse import urlparse

import requests

from pipeline.collectors.firecrawl_collector import FIRECRAWL_BASE, FirecrawlCollector
from pipeline.collectors.reddit_collector import _sub_from_seed
from pipeline.orchestrate import normalize_seed, pick_collector

FIRECRAWL_SEARCH = f"{FIRECRAWL_BASE}/search"
_REDDIT_THREAD = re.compile(r"/r/([A-Za-z0-9_]+)/comments/([A-Za-z0-9]+)/([^/?#]+)/?", re.I)


class DiscoverError(RuntimeError):
    pass


@dataclass
class Candidate:
    url: str
    title: str = ""
    description: str = ""
    collector: Optional[str] = None
    verdict: str = "pending"       # pending | legit | weak | reject | error
    distinct_authors: int = 0
    sample_docs: int = 0
    thread_count: int = 0
    authored: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def firecrawl_search(topic: str, limit: int = 8, api_key: str = None,
                     timeout: int = 60) -> List[Candidate]:
    """Firecrawl web search biased toward communities. Returns unvalidated candidates."""
    key = api_key or os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise DiscoverError("FIRECRAWL_API_KEY missing; topic discovery needs Firecrawl.")
    query = f"{topic} forum community discussion"
    r = requests.post(
        FIRECRAWL_SEARCH,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "limit": limit, "sources": [{"type": "web"}]},
        timeout=timeout,
    )
    if r.status_code >= 400:
        body = (r.text or "").strip().replace("\n", " ")[:400]
        hint = " Check FIRECRAWL_API_KEY." if r.status_code == 401 else ""
        raise DiscoverError(f"Firecrawl search failed: HTTP {r.status_code}. {body}{hint}")
    web = ((r.json() or {}).get("data") or {}).get("web") or []
    out: List[Candidate] = []
    seen = set()
    for w in web:
        url = (w or {}).get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(Candidate(url=url, title=w.get("title") or "",
                              description=w.get("description") or ""))
    return out


def firecrawl_web_search(query: str, limit: int = 8, api_key: str = None,
                         timeout: int = 60) -> list[str]:
    key = api_key or os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise DiscoverError("FIRECRAWL_API_KEY missing; search assist needs Firecrawl.")
    r = requests.post(
        FIRECRAWL_SEARCH,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "limit": limit, "sources": [{"type": "web"}]},
        timeout=timeout,
    )
    if r.status_code >= 400:
        body = (r.text or "").strip().replace("\n", " ")[:400]
        hint = " Check FIRECRAWL_API_KEY." if r.status_code == 401 else ""
        raise DiscoverError(f"Firecrawl search failed: HTTP {r.status_code}. {body}{hint}")
    web = ((r.json() or {}).get("data") or {}).get("web") or []
    return [(w or {}).get("url") for w in web if (w or {}).get("url")]


def _canonical_reddit_thread_url(url: str, subreddit: str | None = None) -> str | None:
    try:
        p = urlparse(normalize_seed(url))
    except Exception:
        return None
    host = (p.netloc or "").lower()
    if not host.endswith("reddit.com"):
        return None
    m = _REDDIT_THREAD.search(p.path or "")
    if not m:
        return None
    sub = m.group(1).lower()
    if subreddit and sub != subreddit.lower():
        return None
    return f"https://www.reddit.com/r/{sub}/comments/{m.group(2)}/{m.group(3)}/"


def discover_reddit_thread_urls(seed: str, limit: int = 40, api_key: str = None,
                                timeout: int = 60, query_templates: list[str] | None = None,
                                exclude_thread_urls: set[str] | None = None) -> list[str]:
    subreddit = _sub_from_seed(seed)
    if not subreddit:
        return []
    templates = query_templates or ["site:reddit.com/r/{sub}/comments/"]
    per_query_limit = max(5, int(limit or 40))
    seen = {u for u in (exclude_thread_urls or set()) if u}
    out: list[str] = []
    for tpl in templates:
        query = tpl.format(sub=subreddit)
        for url in firecrawl_web_search(query, limit=per_query_limit, api_key=api_key, timeout=timeout):
            canon = _canonical_reddit_thread_url(url, subreddit=subreddit)
            if not canon or canon in seen:
                continue
            seen.add(canon)
            out.append(canon)
            if len(out) >= limit:
                return out
    return out


def _structural_thread_count(seed: str, cfg: dict) -> int:
    """Cheap forum-shape probe for generic sources: map + thread-pattern filter, no scrape."""
    coll = cfg.get("collection", {})
    fc = FirecrawlCollector(
        thread_pattern=coll.get("thread_url_pattern", "/t/"),
        thread_patterns=coll.get("thread_url_patterns"),
        same_domain_only=coll.get("same_domain_only", True),
    )
    return len(fc._thread_urls(seed))


def validate_candidate(cand: Candidate, cfg: dict, min_authors: int = 3,
                       probe_limit: int = 5, min_threads: int = 5,
                       max_probe_docs: int = 25) -> Candidate:
    """Judge one candidate. Mutates and returns it. Never raises for a single bad seed."""
    seed = normalize_seed(cand.url)
    try:
        collector, kind = pick_collector(seed, cfg)
    except ValueError as e:            # unsupported-domain policy, etc.
        cand.verdict, cand.reason = "reject", str(e)[:200]
        return cand
    cand.collector = kind
    # Source-agnostic: ask the collector whether it yields per-post authors, rather than
    # naming platforms. A new authored collector inherits granularity="post" for free.
    cand.authored = getattr(collector, "granularity", "post") == "post"

    if cand.authored:
        authors, docs = set(), 0
        try:
            for doc in collector.collect(seed, probe_limit):
                docs += 1
                if doc.author:
                    authors.add(doc.author)
                if docs >= max_probe_docs:
                    break
        except Exception as e:
            cand.verdict, cand.reason, cand.sample_docs = "error", str(e)[:200], docs
            return cand
        cand.sample_docs, cand.distinct_authors = docs, len(authors)
        if docs == 0:
            cand.verdict, cand.reason = "reject", "no posts reachable"
        elif len(authors) >= min_authors:
            cand.verdict = "legit"
            cand.reason = f"{len(authors)} distinct authors in sample of {docs}"
        elif len(authors) >= 1:
            cand.verdict = "weak"
            cand.reason = f"only {len(authors)} author(s) in sample of {docs}"
        else:
            cand.verdict, cand.reason = "reject", "authored source but no authors parsed"
        return cand

    # Generic thread-level: can't prove authorship cheaply, check community structure.
    try:
        n = _structural_thread_count(seed, cfg)
    except Exception as e:
        cand.verdict, cand.reason = "error", str(e)[:200]
        return cand
    cand.thread_count = n
    if n >= min_threads:
        cand.verdict = "weak"
        cand.reason = f"{n} threads found; authorship unverified (thread-level source)"
    else:
        cand.verdict, cand.reason = "reject", f"no community structure ({n} threads found)"
    return cand
