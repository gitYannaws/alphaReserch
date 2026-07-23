"""Arctic Shift collector: full-history Reddit from the public archive, zero reddit.com load.

Arctic Shift (github.com/ArthurHeitmann/arctic_shift) is the community successor to
Pushshift: it continuously archives every public Reddit post + comment and exposes them via

  GET https://arctic-shift.photon-reddit.com/api/posts/search
  GET https://arctic-shift.photon-reddit.com/api/comments/search
      ?subreddit=X&limit=100&sort=asc&after=<epoch>

Why this exists: the live Reddit path (Playwright over old.reddit HTML) is politeness-capped
at 4-9s/request with mandated cooldowns, and Reddit listings hard-cap at ~1000 items - the
`backfill_sort_cycle` rotation exists purely to squeeze around that cap and still cannot
reach full history. The archive has no listing cap and carries strictly better data than the
HTML crawl: exact `created_utc`, `score` on every post AND comment (feeds the stage-6 upvote
signal), per-item `author`. Years of history in minutes, no ban risk.

Two modes, picked automatically:
- FILE mode: if `dump_dir` holds files for the subreddit (from Arctic Shift's download tool
  or an Academic Torrents per-subreddit repack; NDJSON, optionally .zst-compressed), stream
  those. Dumps use zstd --long=31, so decompression sets max_window_log.
- API mode: otherwise sweep the API ascending by created_utc, cursor-paginated. "A couple
  requests per second" is explicitly fine per their docs; default pause stays under that,
  and 429s honor the advertised reset.

Trade-offs stated plainly: third-party archive of public content (standard in research,
never blessed by Reddit - same gray zone as the HTML crawl, minus the load on reddit.com);
dumps/API trail live Reddit by up to minutes-to-weeks (the live refresh path still owns the
fresh edge); the archive preserves since-deleted content, so the repo privacy rule (salted
author_hash, raw text treated as identifying) matters extra here.
"""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional
from urllib.parse import urlparse

import requests

from .base import Collector, Document

API_BASE = "https://arctic-shift.photon-reddit.com"
UA = {"User-Agent": "sales-safari/1.0 (market research; pain mining)"}
_DELETED = {"[deleted]", "[removed]", "", None}


def _sub_from_seed(seed: str) -> Optional[str]:
    m = re.search(r"/r/([A-Za-z0-9_]+)", urlparse(seed).path)
    return m.group(1) if m else None


def _canon(permalink: str) -> str:
    if not permalink:
        return permalink
    if permalink.startswith("http"):
        return permalink
    return "https://www.reddit.com" + permalink


def _iso(created_utc) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(created_utc), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _score(item: dict) -> Optional[int]:
    try:
        return int(item.get("score"))
    except (TypeError, ValueError):
        return None


class ArcticShiftError(RuntimeError):
    pass


class ArcticShiftCollector(Collector):
    source_type = "forum"  # same shape as the live Reddit collector's output
    granularity = "post"

    def __init__(self, api_base: str = API_BASE, page_size: int = 100, pause: float = 0.7,
                 timeout: int = 30, min_comment_len: int = 20, min_post_len: int = 25,
                 max_comments: int = 0, dump_dir: str = "", progress_cb=None):
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.page_size = max(1, min(100, int(page_size or 100)))  # API caps limit at 100
        self.pause = pause
        self.timeout = timeout
        self.min_comment_len = min_comment_len
        self.min_post_len = min_post_len
        self.max_comments = max(0, int(max_comments or 0))  # 0 = unlimited
        self.dump_dir = dump_dir or ""
        self.progress_cb = progress_cb

    def _beat(self, phase: str, **meta):
        if self.progress_cb:
            self.progress_cb(phase, meta)

    # ---- document mapping (shared by API + file modes) ----
    def _submission_doc(self, s: dict, sub: str) -> Optional[Document]:
        if s.get("author") in _DELETED:
            return None
        title = (s.get("title") or "").strip()
        body = (s.get("selftext") or "").strip()
        if body in ("[deleted]", "[removed]"):
            body = ""
        text = f"{title}\n\n{body}".strip()
        if len(text) < self.min_post_len:
            return None
        permalink = s.get("permalink") or f"/r/{sub}/comments/{s.get('id')}/"
        url = _canon(permalink)
        return Document(
            source_type=self.source_type, source_url=url, permalink=url,
            title=title, raw_markdown=text, source_granularity="post",
            author=s.get("author"), thread_url=url,
            created_at=_iso(s.get("created_utc")), score=_score(s),
        )

    def _comment_doc(self, c: dict, sub: str) -> Optional[Document]:
        author = c.get("author")
        body = (c.get("body") or "").strip()
        if author in _DELETED or body in ("[deleted]", "[removed]"):
            return None
        if len(body) < self.min_comment_len:
            return None
        link_id = str(c.get("link_id") or "").removeprefix("t3_")
        permalink = c.get("permalink") or (
            f"/r/{sub}/comments/{link_id}/_/{c.get('id')}/" if link_id and c.get("id") else "")
        if not permalink:
            return None
        url = _canon(permalink)
        thread_url = _canon(f"/r/{sub}/comments/{link_id}/") if link_id else None
        return Document(
            source_type=self.source_type, source_url=url, permalink=url,
            title="", raw_markdown=body, source_granularity="post",
            author=author, thread_url=thread_url,
            created_at=_iso(c.get("created_utc")), score=_score(c),
        )

    # ---- API mode ----
    def _get_page(self, kind: str, sub: str, after: int) -> List[dict]:
        # The live API 400s on after=0 ("'after' must be a valid date"); the first page
        # simply omits the cursor.
        cursor = f"&after={after}" if after > 0 else ""
        url = (f"{self.api_base}/api/{kind}/search"
               f"?subreddit={sub}&limit={self.page_size}&sort=asc{cursor}")
        for attempt in range(4):
            try:
                resp = requests.get(url, headers=UA, timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                raise ArcticShiftError(f"arctic shift connection error: {e}")
            if resp.status_code == 429:
                # Their docs promise headroom at a couple req/s; a 429 means back off for
                # the advertised reset (or a growing default) and retry the same page.
                try:
                    wait = max(1.0, float(resp.headers.get("X-RateLimit-Reset") or 0))
                except ValueError:
                    wait = 0.0
                time.sleep(wait or (2.0 * (attempt + 1)))
                continue
            if resp.status_code != 200:
                raise ArcticShiftError(f"arctic shift HTTP {resp.status_code}: {resp.text[:200]}")
            j = resp.json()
            data = j.get("data") if isinstance(j, dict) else j
            return data if isinstance(data, list) else []
        raise ArcticShiftError("arctic shift kept returning 429 after 4 attempts")

    def _sweep_api(self, kind: str, sub: str, cap: int) -> Iterator[dict]:
        """Ascending created_utc cursor sweep. `after` is inclusive-boundary-fuzzy, so the
        cursor re-fetches the last second and `seen` ids drop the overlap; if a page of
        same-second items fails to move the cursor, it is forced forward one second rather
        than looping forever (pathological: >page_size items in one second)."""
        after, seen, yielded = 0, set(), 0
        while True:
            if cap and yielded >= cap:
                return
            page = self._get_page(kind, sub, after)
            if not page:
                return
            fresh = [it for it in page if it.get("id") not in seen]
            for it in fresh:
                seen.add(it.get("id"))
                yield it
                yielded += 1
                if cap and yielded >= cap:
                    return
            last_ts = 0
            for it in page:
                try:
                    last_ts = max(last_ts, int(it.get("created_utc") or 0))
                except (TypeError, ValueError):
                    pass
            next_after = max(last_ts - 1, 0)
            if not fresh or next_after <= after:
                next_after = max(after, last_ts) + 1  # force progress
            after = next_after
            self._beat(f"{kind}-sweep", count=yielded)
            time.sleep(self.pause)

    # ---- file mode ----
    def _dump_files(self, sub: str) -> dict:
        """{kind: path} for dump files matching this subreddit in dump_dir. Accepts the
        download tool's / torrent repacks' common namings, .zst or plain NDJSON."""
        found = {}
        root = Path(self.dump_dir)
        if not self.dump_dir or not root.is_dir():
            return found
        low = sub.lower()
        for p in root.iterdir():
            name = p.name.lower()
            if low not in name or not p.is_file():
                continue
            if name.endswith((".zst", ".jsonl", ".ndjson", ".json")):
                if "submission" in name or "posts" in name:
                    found.setdefault("posts", p)
                elif "comment" in name:
                    found.setdefault("comments", p)
        return found

    def _stream_file(self, path: Path) -> Iterator[dict]:
        def _lines(fh):
            buf = b""
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        yield line
            if buf.strip():
                yield buf

        if str(path).endswith(".zst"):
            try:
                import zstandard
            except ImportError as e:
                raise ArcticShiftError(
                    "zstandard is not installed; run .venv/Scripts/python -m pip install zstandard") from e
            with open(path, "rb") as raw:
                # Dumps are compressed with --long=31; a default-window reader errors out.
                dctx = zstandard.ZstdDecompressor(max_window_size=2 ** 31)
                with dctx.stream_reader(raw) as fh:
                    for line in _lines(fh):
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue
        else:
            with open(path, "rb") as fh:
                for line in _lines(fh):
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue

    # ---- main ----
    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        sub = _sub_from_seed(seed_url)
        if not sub:
            raise ArcticShiftError(
                f"could not parse a subreddit from {seed_url}; arctic shift needs /r/<sub>")
        files = self._dump_files(sub)
        posts = comments = 0
        if files:
            self._beat("dump-files", files={k: str(v) for k, v in files.items()})
            if "posts" in files:
                for s in self._stream_file(files["posts"]):
                    if limit and posts >= limit:
                        break
                    doc = self._submission_doc(s, sub)
                    if doc:
                        posts += 1
                        yield doc
            if "comments" in files:
                for c in self._stream_file(files["comments"]):
                    if self.max_comments and comments >= self.max_comments:
                        break
                    doc = self._comment_doc(c, sub)
                    if doc:
                        comments += 1
                        yield doc
            return
        # API mode. Submissions honor `limit` (the run's thread cap); comments sweep the
        # whole subreddit (capped only by max_comments) - a comment's Document stands alone,
        # so it does not need its submission to have been yielded first.
        for s in self._sweep_api("posts", sub, cap=limit or 0):
            doc = self._submission_doc(s, sub)
            if doc:
                posts += 1
                yield doc
        for c in self._sweep_api("comments", sub, cap=self.max_comments):
            doc = self._comment_doc(c, sub)
            if doc:
                comments += 1
                yield doc
        self._beat("done", posts=posts, comments=comments)
