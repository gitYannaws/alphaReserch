"""Corpus-quality guards for the render path (run e81aed813e0f regressions).

That run collected a XenForo board through Playwright and produced: every thread stored
2-3 times (base / latest / page-2), zero authors, and site nav prepended to every
document. Investigating it also surfaced a truncation bug in the shared post parser --
every reply containing a quote lost the replying author's own words.
"""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.collectors.playwright_collector import (
    BLOCK_MAX_BODY_CHARS, BOILERPLATE_SELECTOR, PlaywrightCollector,
    is_blocked_text, _text_key,
)
from pipeline.collectors.xenforo_collector import (
    base_thread_url, is_xenforo_html, parse_thread_posts,
)

THREAD_HTML = """
<html data-xf-init="thread-view"><head><title>Map of where I&#8217;d live | Geomax.me</title></head>
<body>
<nav>Forums Members Rules What's new Log in Register Search New posts</nav>
<article data-author="Alexander" class="message">
  <time datetime="2025-12-23T10:00:00+0000"></time>
  <div class="message-inner" data-content="post-1">
    <div class="bbWrapper">Add yours also</div>
  </div>
</article>
<article data-author="Xavist" class="message">
  <time datetime="2026-03-30T09:00:00+0000"></time>
  <div class="message-inner" data-content="post-51">
    <div class="bbWrapper"><blockquote data-quote="doomercoded"
        class="bbCodeBlock bbCodeBlock--expandable bbCodeBlock--quote js-expandWatch">
  <div class="bbCodeBlock-title">
    <a href="/goto/post?id=50" class="bbCodeBlock-sourceJump">doomercoded said:</a>
  </div>
  <div class="bbCodeBlock-content">
    <div class="bbCodeBlock-expandContent js-expandContent">tourists are white</div>
    <div class="bbCodeBlock-expandLink js-expandLink"><a role="button">Click to expand...</a></div>
  </div>
</blockquote>Rent there ate my whole salary<br>
and the agency wanted three months up front</div>
  </div>
</article>
<a href="/threads/map-of-where-i-d-live.9700/page-2">2</a>
</body></html>
"""

THREAD = "https://x.me/threads/slug.9700"


class BaseThreadUrlTests(unittest.TestCase):
    def test_collapses_page_latest_and_post_aliases(self):
        for suffix in ("", "/latest", "/page-2", "/post-1234", "/unread"):
            with self.subTest(suffix=suffix):
                self.assertEqual(base_thread_url(THREAD + suffix), THREAD)

    def test_leaves_non_thread_urls_alone(self):
        self.assertEqual(base_thread_url("https://x.me/blog/some-post"),
                         "https://x.me/blog/some-post")


class XenforoHtmlParseTests(unittest.TestCase):
    def test_is_xenforo_html_reads_markers_not_the_network(self):
        self.assertTrue(is_xenforo_html(THREAD_HTML))
        self.assertFalse(is_xenforo_html("<html><body>Checking your browser</body></html>"))

    def test_yields_per_post_authors(self):
        docs = parse_thread_posts(THREAD_HTML, THREAD + "/latest")
        self.assertEqual([d.author for d in docs], ["Alexander", "Xavist"])
        self.assertEqual([d.source_granularity for d in docs], ["post", "post"])
        self.assertEqual([d.permalink for d in docs],
                         [f"{THREAD}#post-1", f"{THREAD}#post-51"])
        self.assertTrue(all(d.thread_url == THREAD for d in docs))
        self.assertEqual(docs[0].created_at, "2025-12-23T10:00:00+0000")

    def test_drops_nav_and_quoted_text(self):
        bodies = " ".join(d.raw_markdown for d in parse_thread_posts(THREAD_HTML, THREAD))
        self.assertNotIn("Log in Register", bodies)      # nav never enters a post body
        self.assertNotIn("doomercoded said", bodies)     # quoted text is another author's
        self.assertNotIn("tourists are white", bodies)
        self.assertNotIn("Click to expand", bodies)

    def test_keeps_the_whole_reply_around_a_quote_block(self):
        # The old `(.*?)</div>\\s*</div>` terminator stopped inside the quote's nested
        # divs, so a reply's own words were dropped and only the quote survived.
        reply = [d for d in parse_thread_posts(THREAD_HTML, THREAD) if d.author == "Xavist"]
        self.assertEqual(len(reply), 1)
        self.assertEqual(reply[0].raw_markdown,
                         "Rent there ate my whole salary and the agency wanted three "
                         "months up front")

    def test_returns_empty_on_unrecognized_html(self):
        self.assertEqual(parse_thread_posts("<html><body>no posts</body></html>", THREAD), [])


class FakeListingPage:
    """Minimal stand-in for a Playwright page over a paginated thread listing.

    `pages` maps a listing URL to the thread hrefs it links. rel="next" is derived from
    the ordering, so the collector has to actually follow it to see later pages.
    """

    def __init__(self, order, pages):
        self.order = order
        self.pages = pages
        self.current = order[0]
        self.visited = [order[0]]

    def goto(self, url):
        self.current = url
        self.visited.append(url)

    def eval_on_selector_all(self, _selector, _js):
        return self.pages[self.current]

    def evaluate(self, _js, *_a):
        i = self.order.index(self.current)
        return self.order[i + 1] if i + 1 < len(self.order) else ""


class ListingPaginationTests(unittest.TestCase):
    ORDER = ["https://x.me/forums/f.2",
             "https://x.me/forums/f.2/page-2",
             "https://x.me/forums/f.2/page-3"]
    PAGES = {
        ORDER[0]: ["/threads/a.1", "/threads/a.1/latest", "/threads/b.2"],
        ORDER[1]: ["/threads/c.3", "/threads/d.4"],
        ORDER[2]: ["/threads/e.5"],
    }

    def _collector(self, max_listing_pages):
        c = PlaywrightCollector(thread_patterns=["/threads/"],
                               max_listing_pages=max_listing_pages)
        c._goto = lambda page, url: page.goto(url)
        c._scroll = lambda page: None
        c._sleep = lambda: None
        return c

    def _walk(self, max_listing_pages, max_docs=100, page=None, skip=None):
        page = page or FakeListingPage(self.ORDER, self.PAGES)
        c = self._collector(max_listing_pages)
        if skip:
            c.skip_thread_urls = skip
        return list(c._iter_listing_threads(page, self.ORDER[0], max_docs)), page

    def test_one_page_is_the_old_seed_only_behaviour(self):
        urls, page = self._walk(1)
        self.assertEqual(urls, ["https://x.me/threads/a.1", "https://x.me/threads/b.2"])
        self.assertEqual(page.visited, [self.ORDER[0]])       # never paginated

    def test_follows_rel_next_and_collapses_alias_links(self):
        urls, page = self._walk(3)
        self.assertEqual(urls, [f"https://x.me/threads/{s}" for s in
                                ("a.1", "b.2", "c.3", "d.4", "e.5")])
        self.assertEqual(page.visited, self.ORDER)

    def test_stops_when_rel_next_runs_out_before_the_cap(self):
        urls, page = self._walk(99)
        self.assertEqual(len(urls), 5)
        self.assertEqual(page.visited, self.ORDER)

    def test_honors_max_docs_mid_page(self):
        urls, _ = self._walk(3, max_docs=3)
        self.assertEqual(urls, [f"https://x.me/threads/{s}" for s in ("a.1", "b.2", "c.3")])

    def test_is_lazy_so_collection_starts_before_discovery_finishes(self):
        # Walking all 254 listings up front means ~an hour before the first document is
        # stored, and discovery lost entirely if the runner dies partway.
        page = FakeListingPage(self.ORDER, self.PAGES)
        gen = self._collector(3)._iter_listing_threads(page, self.ORDER[0], 100)
        self.assertEqual(next(gen), "https://x.me/threads/a.1")
        self.assertEqual(page.visited, [self.ORDER[0]])       # page 2 not fetched yet
        next(gen), next(gen)                                   # exhaust page 1, cross over
        self.assertEqual(page.visited, self.ORDER[:2])

    def test_max_docs_stops_before_fetching_another_listing_page(self):
        page = FakeListingPage(self.ORDER, self.PAGES)
        self._walk(3, max_docs=2, page=page)
        self.assertEqual(page.visited, [self.ORDER[0]])

    def test_skips_thread_roots_already_in_the_corpus(self):
        # An interrupted deep backfill must resume, not re-walk 5000 threads.
        urls, _ = self._walk(3, skip={"https://x.me/threads/a.1",
                                      "https://x.me/threads/c.3"})
        self.assertEqual(urls, [f"https://x.me/threads/{s}" for s in ("b.2", "d.4", "e.5")])

    def test_skip_list_is_normalized_from_alias_urls(self):
        c = PlaywrightCollector(
            thread_patterns=["/threads/"],
            skip_thread_urls=["https://x.me/threads/a.1/page-2/",
                              "https://x.me/threads/b.2/latest"])
        self.assertEqual(c.skip_thread_urls,
                         {"https://x.me/threads/a.1", "https://x.me/threads/b.2"})

    def test_skips_offsite_next_link(self):
        order = ["https://x.me/forums/f.2", "https://evil.example/forums/f.2/page-2"]
        page = FakeListingPage(order, {order[0]: ["/threads/a.1"], order[1]: []})
        urls = list(self._collector(5)._iter_listing_threads(page, order[0], 100))
        self.assertEqual(urls, ["https://x.me/threads/a.1"])
        self.assertEqual(page.visited, [order[0]])


class BlockedPageDetectionTests(unittest.TestCase):
    """A member writing "forbidden" is not a bot wall (this aborted a 254-page crawl)."""

    def test_content_mentioning_blocked_words_is_not_a_wall(self):
        post = ("taking trips to the vatican monthly to see if there is any forbidden "
                "knowledge or shady characters popping up. ")
        thread = "Having a gf and kids is bare minimum " + post * 40
        self.assertGreater(len(thread), BLOCK_MAX_BODY_CHARS)
        self.assertFalse(is_blocked_text(thread))

    def test_real_walls_are_still_caught(self):
        for wall in ("Access denied. You do not have permission.",
                     "Please complete the captcha to continue.",
                     "403 Forbidden",
                     "Too many requests. Slow down.",
                     "Login required to view this page.",
                     "Checking your browser. Verifying your connection."):
            with self.subTest(wall=wall):
                self.assertTrue(is_blocked_text(wall))

    def test_marker_below_the_scan_window_of_a_short_page_is_ignored(self):
        # Short page, but the word appears late -- a wall says it up front.
        self.assertFalse(is_blocked_text("x" * 800 + " forbidden"))

    def test_empty_and_none_are_not_walls(self):
        self.assertFalse(is_blocked_text(""))
        self.assertFalse(is_blocked_text(None))


class DedupeHelperTests(unittest.TestCase):
    def test_boilerplate_selector_covers_the_chrome_that_leaked(self):
        for sel in ("nav", "header", "footer", "blockquote", "script"):
            self.assertIn(sel, BOILERPLATE_SELECTOR)

    def test_text_key_dedupes_byte_identical_pages(self):
        # /latest and /page-2 came back byte-identical on the offending run.
        self.assertEqual(_text_key("same body"), _text_key("same body"))
        self.assertNotEqual(_text_key("same body"), _text_key("other body"))


if __name__ == "__main__":
    unittest.main()
