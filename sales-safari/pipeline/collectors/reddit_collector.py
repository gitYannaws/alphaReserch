"""Reddit collector via headless Playwright over old.reddit.com HTML.

Reddit's public `.json` endpoints now return 403 "blocked by network security" to any
non-residential client - even a real browser hitting the .json path - so the old plain
`requests` JSON path is dead from datacenter IPs. old.reddit.com HTML, however, still
renders (200), so we load THAT with Playwright and parse the server-rendered DOM.

Reddit stays a PER-COMMENT source: each comment (and the submission body) becomes its
own post-level Document with its author attached - the distinct-author signal §5/§6
depend on. old.reddit's stable DOM makes this reliable:

  listing : https://old.reddit.com/r/{sub}/{sort}/?t={time}     -> #siteTable div.thing.link
  thread  : https://old.reddit.com{permalink}                   -> .commentarea div.thing.comment

This is a renderer, not an evasion layer: real browser UA, an over18=1 age-ack cookie,
no stealth/proxy/CAPTCHA solving. Stops on 403/429, login walls, and bot-wall text.
"""
import random
import re
import time
from typing import Callable, Iterator, List, Optional
from urllib.parse import parse_qs, urlparse

from .base import Collector, Document

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_WS = re.compile(r"\s+")
_DELETED = {"[deleted]", "[removed]", "", None}
BLOCKED_TEXT = (
    "blocked by network security",
    "verify you are human",
    "captcha",
    "access denied",
    "too many requests",
    "log in to continue",
    "sign in to continue",
)
SORT_ALIASES = {
    "best": "hot",  # old.reddit's subreddit root behaves like the "best/hot" feed here
    "hot": "hot",
    "new": "new",
    "top": "top",
    "controversial": "controversial",
}

# DOM extraction (old.reddit). Runs in the page; returns JSON-serialisable data.
_LISTING_JS = """() => {
  const out=[];
  document.querySelectorAll('#siteTable div.thing.link').forEach(t=>{
    if(t.classList.contains('promoted')) return;
    const permalink=t.getAttribute('data-permalink');
    if(permalink) out.push({permalink, title:(t.querySelector('a.title')?.innerText||'').trim()});
  });
  const next=document.querySelector('span.next-button a')?.href || null;
  return {threads: out, next};
}"""

_THREAD_JS = """() => {
  // net score from old.reddit's midcol/tagline. The .unvoted span's title holds the exact
  // int; "score hidden" (new comments) has no numeric title -> null. Scope to the passed
  // root so a comment never picks up a reply's score.
  const scoreOf = (root) => {
    if(!root) return null;
    const el = root.querySelector('.score.unvoted') || root.querySelector('.score.likes')
             || root.querySelector('.score');
    const n = el ? parseInt(el.getAttribute('title'), 10) : NaN;
    return Number.isFinite(n) ? n : null;
  };
  const link=document.querySelector('#siteTable div.thing.link');
  const op = link ? {
    author: link.getAttribute('data-author'),
    title: (document.querySelector('a.title')?.innerText||'').trim(),
    body: (link.querySelector('.entry .usertext-body .md')?.innerText||'').trim(),
    permalink: link.getAttribute('data-permalink'),
    datetime: link.querySelector('.entry time')?.getAttribute('datetime') || null,
    score: scoreOf(link.querySelector('.midcol'))
  } : null;
  const comments=[];
  document.querySelectorAll('.commentarea div.thing.comment').forEach(c=>{
    const author=c.getAttribute('data-author');
    if(!author || author==='[deleted]' || author==='[removed]') return;
    const entry=c.querySelector(':scope > .entry');          // this comment's own body, not replies'
    const body=(entry?.querySelector('.usertext-body .md')?.innerText||'').trim();
    comments.push({author, body, permalink:c.getAttribute('data-permalink'),
                   datetime:entry?.querySelector('time')?.getAttribute('datetime')||null,
                   score:scoreOf(entry?.querySelector(':scope > .tagline'))});
  });
  return {op, comments};
}"""


class RedditCollectorError(RuntimeError):
    pass


def _is_retry_block(msg: str) -> bool:
    low = (msg or "").lower()
    return ("http 429" in low or "http 403" in low or "bot/login wall" in low
            or "access denied" in low or "too many requests" in low)


def _clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def _sub_from_seed(seed: str) -> Optional[str]:
    m = re.search(r"/r/([A-Za-z0-9_]+)", urlparse(seed).path)
    return m.group(1) if m else None


def _seed_listing_prefs(seed: str) -> tuple[Optional[str], Optional[str]]:
    """Infer listing sort/time filter from a subreddit seed URL when explicitly present."""
    p = urlparse(seed)
    parts = [seg for seg in p.path.split("/") if seg]
    sort = None
    if len(parts) >= 3 and parts[0].lower() == "r":
        sort = SORT_ALIASES.get(parts[2].lower())
    q = parse_qs(p.query or "")
    time_filter = (q.get("t") or [None])[0]
    return sort, time_filter


def _canon(permalink: str) -> str:
    """A /r/.../comments/... path -> canonical www permalink."""
    if not permalink:
        return permalink
    if permalink.startswith("http"):
        return permalink
    return "https://www.reddit.com" + permalink


class RedditCollector(Collector):
    source_type = "forum"
    granularity = "post"          # per-comment authored

    def __init__(self, keywords: Optional[List[str]] = None, sort: str = "hot",
                 time_filter: str = "year", max_comments_per_thread: int = 200,
                 comment_depth: int = 8, min_comment_len: int = 20,
                 min_delay_seconds: float = 1.2, max_delay_seconds: float = 2.5,
                 timeout: int = 30, headless: bool = True, timeout_ms: int = 30000,
                 skip_thread_urls: Optional[set[str]] = None,
                 stale_listing_pages: int = 3, max_listing_pages: int = 36,
                 extra_thread_urls: Optional[List[str]] = None,
                 sort_plan: Optional[List[tuple[str, Optional[str]]]] = None,
                 cooldown_every_seconds: float = 0, cooldown_seconds: float = 0,
                 progress_cb: Optional[Callable[[str, dict], None]] = None):
        self.keywords = [k.lower() for k in (keywords or [])]
        self.sort = SORT_ALIASES.get((sort or "hot").lower(), "hot")
        self.time_filter = time_filter
        self.sort_plan = [
            (SORT_ALIASES.get((s or "hot").lower(), "hot"), tf)
            for s, tf in (sort_plan or [(self.sort, time_filter)])
        ]
        self.max_comments_per_thread = max_comments_per_thread
        self.comment_depth = comment_depth          # kept for signature compat; DOM sets depth
        self.min_comment_len = min_comment_len
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.skip_thread_urls = {self._canon_thread(u) for u in (skip_thread_urls or set()) if u}
        self.extra_thread_urls = [self._canon_thread(u) for u in (extra_thread_urls or []) if u]
        self.stale_listing_pages = max(1, int(stale_listing_pages or 3))
        self.max_listing_pages = max(1, int(max_listing_pages or 36))
        self.cooldown_every_seconds = max(0.0, float(cooldown_every_seconds or 0))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds or 0))
        now = time.monotonic()
        self._started_at = now
        self._last_cooldown_at = now
        self._last_listing_raw = 0
        self.progress_cb = progress_cb

    # ---- detection ----
    @staticmethod
    def is_reddit(url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(host == h or host.endswith(f".{h}") for h in ("reddit.com",))

    # ---- helpers ----
    def _sleep(self):
        self._maybe_cooldown()
        time.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))

    def _maybe_cooldown(self):
        if self.cooldown_every_seconds <= 0 or self.cooldown_seconds <= 0:
            return
        now = time.monotonic()
        if now - self._last_cooldown_at < self.cooldown_every_seconds:
            return
        remaining = int(self.cooldown_seconds)
        while remaining > 0:
            self._beat("cooldown", remaining_seconds=remaining)
            nap = min(30, remaining)
            time.sleep(nap)
            remaining -= nap
        self._last_cooldown_at = time.monotonic()
        self._beat("cooldown-done")

    def _keep_title(self, title: str) -> bool:
        if not self.keywords:
            return True
        t = (title or "").lower()
        # word-boundary match so "cut" doesn't match "cute" / "material" not "materialism".
        return any(re.search(rf"\b{re.escape(k)}\b", t) for k in self.keywords)

    @staticmethod
    def _old(url_or_path: str) -> str:
        """Force a URL/path onto the old.reddit.com host (server-rendered HTML)."""
        if url_or_path.startswith("http"):
            p = urlparse(url_or_path)
            return f"https://old.reddit.com{p.path}" + (f"?{p.query}" if p.query else "")
        return "https://old.reddit.com" + url_or_path

    @staticmethod
    def _canon_thread(url_or_path: str) -> str:
        return _canon(urlparse(url_or_path).path if url_or_path.startswith("http") else url_or_path)

    def _listing_url(self, sub: str, sort: Optional[str] = None,
                     time_filter: Optional[str] = None) -> str:
        sort = SORT_ALIASES.get((sort or self.sort or "hot").lower(), "hot")
        time_filter = time_filter or self.time_filter
        base = f"https://old.reddit.com/r/{sub}/"
        if sort in ("new", "top", "controversial"):
            base += f"{sort}/"
        if sort in ("top", "controversial") and time_filter:
            base += f"?t={time_filter}"
        return base

    def _beat(self, phase: str, **meta):
        if self.progress_cb:
            self.progress_cb(phase, meta)

    def _goto(self, page, url: str):
        self._beat("goto", url=url)
        resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        status = resp.status if resp else None
        if status in (403, 404, 429):
            raise RedditCollectorError(
                f"HTTP {status} from old.reddit for {url}; stopping (blocked/rate-limited - "
                "not evading). Reddit may require a residential IP or OAuth here."
            )
        body = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        low = body.lower()
        if any(m in low for m in BLOCKED_TEXT):
            raise RedditCollectorError(
                f"bot/login wall reached at {url}; stopping (not evading)."
            )
        self._beat("loaded", url=url, status=status or 200)
        return body

    # ---- enumeration ----
    def _list_threads(self, page, sub: str, limit: int, seen_permalinks: Optional[set[str]] = None) -> List[dict]:
        threads, seen = [], set(seen_permalinks or set())
        max_pages = self.max_listing_pages
        # Raw thing.link count across all listing pages, BEFORE skip/keyword filtering.
        # Lets collect() tell "listing empty/blocked" (real error) from "listing full but
        # every thread already collected" (benign no-op on a corpus refresh).
        self._last_listing_raw = 0
        plan = self.sort_plan or [(self.sort, self.time_filter)]
        for idx, (sort, time_filter) in enumerate(plan):
            self.sort = sort
            self.time_filter = time_filter or self.time_filter
            self._beat("sort-start", sort=sort, time_filter=time_filter,
                       sort_index=idx + 1, sort_total=len(plan))
            url, guard, stale_pages = self._listing_url(sub, sort, time_filter), 0, 0
            while len(threads) < limit and url and guard < max_pages:
                guard += 1
                self._goto(page, url)
                data = page.evaluate(_LISTING_JS) or {}
                before = len(threads)
                page_raw = data.get("threads") or []
                self._last_listing_raw += len(page_raw)
                for t in page_raw:
                    perm = t.get("permalink")
                    canon = self._canon_thread(perm) if perm else None
                    if (perm and perm not in seen and canon not in self.skip_thread_urls
                            and self._keep_title(t.get("title", ""))):
                        seen.add(perm)
                        threads.append(t)
                if self.skip_thread_urls:
                    stale_pages = stale_pages + 1 if len(threads) == before else 0
                    if stale_pages >= self.stale_listing_pages:
                        self._beat("listing-stale-stop", sort=sort, page=guard,
                                   discovered=len(threads), stale_pages=stale_pages)
                        break
                self._beat("listing", sort=sort, page=guard, discovered=len(threads),
                           added=len(threads) - before, stale_pages=stale_pages,
                           next=bool(data.get("next")))
                nxt = data.get("next")
                url = self._old(nxt) if nxt else None
                if url:
                    self._sleep()
            if len(threads) >= limit:
                break
            if idx + 1 < len(plan):
                self._beat("sort-rotate", sort=sort, next_sort=plan[idx + 1][0],
                           discovered=len(threads))
        return threads[:limit]

    # ---- thread parse ----
    def _thread_docs(self, page, permalink: str) -> Iterator[Document]:
        self._beat("thread-start", permalink=_canon(permalink))
        self._goto(page, self._old(permalink))
        data = page.evaluate(_THREAD_JS) or {}
        op = data.get("op")
        if op and op.get("author") not in _DELETED:
            body = _clean(op.get("body", ""))
            if len(body) >= self.min_comment_len:
                url = _canon(op.get("permalink") or permalink)
                yield Document(
                    source_type=self.source_type, source_url=url, permalink=url,
                    title=op.get("title", ""), raw_markdown=body,
                    source_granularity="post", author=op.get("author"),
                    thread_url=url, created_at=op.get("datetime"), score=op.get("score"),
                )
        title = (op or {}).get("title", "")
        thread_url = _canon((op or {}).get("permalink") or permalink)
        count = 0
        for c in data.get("comments") or []:
            if count >= self.max_comments_per_thread:
                break
            author = c.get("author")
            body = _clean(c.get("body", ""))
            if author in _DELETED or len(body) < self.min_comment_len:
                continue
            count += 1
            url = _canon(c.get("permalink"))
            yield Document(
                source_type=self.source_type, source_url=url, permalink=url,
                title=title, raw_markdown=body, source_granularity="post",
                author=author, thread_url=thread_url, created_at=c.get("datetime"),
                score=c.get("score"),
            )
        self._beat("thread-done", permalink=thread_url, comments=count)

    # ---- main ----
    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RedditCollectorError(
                "Playwright is not installed. Run: .venv/Scripts/python -m pip install playwright "
                "and then .venv/Scripts/python -m playwright install chromium"
            ) from e

        is_thread = "/comments/" in seed_url
        sub = None
        if not is_thread:
            sub = _sub_from_seed(seed_url)
            if not sub:
                raise RedditCollectorError(
                    f"could not parse a subreddit from {seed_url}; expected /r/<sub>/ or a "
                    "/comments/ thread URL."
                )

        with sync_playwright() as pw:
            self._beat("launching-browser", headless=self.headless)
            browser = pw.chromium.launch(headless=self.headless)
            try:
                self._beat("browser-ready")
                ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
                ctx.add_cookies([{"name": "over18", "value": "1",
                                  "domain": ".reddit.com", "path": "/"}])
                page = ctx.new_page()

                if is_thread:
                    yield from self._thread_docs(page, urlparse(seed_url).path)
                    return

                seed_threads = []
                seen_seed_permalinks = set()
                for turl in self.extra_thread_urls:
                    perm = urlparse(turl).path if turl.startswith("http") else turl
                    if perm and perm not in seen_seed_permalinks:
                        seen_seed_permalinks.add(perm)
                        seed_threads.append({"permalink": perm, "title": ""})
                if seed_threads:
                    self._beat("search-assist", discovered=len(seed_threads))
                listing_limit = max(0, limit - len(seed_threads))
                threads = seed_threads[:limit]
                if len(threads) < limit:
                    threads.extend(self._list_threads(page, sub, listing_limit, seen_permalinks=seen_seed_permalinks))
                if not threads:
                    if self._last_listing_raw > 0:
                        # Listing rendered fine, but every thread was already collected (corpus
                        # refresh) or filtered out by keywords. Not an error - just nothing new.
                        self._beat("listing-nothing-new", raw_seen=self._last_listing_raw)
                        return
                    raise RedditCollectorError(
                        f"no threads found for r/{sub} (sort={self.sort}); the listing returned "
                        "no posts - check the subreddit name, or Reddit may be blocking/rate-"
                        "limiting this client."
                    )
                for t in threads:
                    self._sleep()
                    try:
                        yield from self._thread_docs(page, t["permalink"])
                    except RedditCollectorError as e:
                        if "http 404" in str(e).lower():
                            print(f"  skip thread {t.get('permalink')}: {e}")
                            continue
                        raise
                    except Exception as e:
                        print(f"  skip thread {t.get('permalink')}: {e}")
                        continue
            finally:
                browser.close()
