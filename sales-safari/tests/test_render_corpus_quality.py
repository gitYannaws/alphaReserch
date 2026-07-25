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

from pipeline.collectors.playwright_collector import BOILERPLATE_SELECTOR, _text_key
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
