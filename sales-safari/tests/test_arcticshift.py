"""Tests for the Arctic Shift collector (Reddit historical via the public archive)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.collectors.arcticshift_collector import ArcticShiftCollector, _sub_from_seed
from pipeline.orchestrate import pick_collector


def _c(**kw):
    kw.setdefault("pause", 0)
    return ArcticShiftCollector(**kw)


SUB = {"id": "p1", "title": "Wire fees are killing me",
       "selftext": "Every transfer costs a fortune and takes days.",
       "author": "alice", "created_utc": 1700000000, "score": 42,
       "permalink": "/r/testsub/comments/p1/wire_fees/"}
COM = {"id": "c1", "body": "Same here, I switched banks twice over this and it never helps.",
       "author": "bob", "created_utc": 1700000100, "score": 7,
       "link_id": "t3_p1", "permalink": "/r/testsub/comments/p1/wire_fees/c1/"}


class MappingTest(unittest.TestCase):
    def test_seed_parse(self):
        self.assertEqual(_sub_from_seed("https://www.reddit.com/r/thepassportbros/top/"),
                         "thepassportbros")
        self.assertIsNone(_sub_from_seed("https://example.com/forum/"))

    def test_submission_maps_with_score_author_timestamp(self):
        d = _c()._submission_doc(SUB, "testsub")
        self.assertEqual(d.author, "alice")
        self.assertEqual(d.score, 42)
        self.assertEqual(d.created_at, "2023-11-14T22:13:20+00:00")  # exact, from epoch
        self.assertEqual(d.permalink, "https://www.reddit.com/r/testsub/comments/p1/wire_fees/")
        self.assertEqual(d.thread_url, d.permalink)
        self.assertIn("Wire fees", d.raw_markdown)
        self.assertIn("Every transfer", d.raw_markdown)

    def test_comment_maps_and_builds_thread_url_from_link_id(self):
        d = _c()._comment_doc(COM, "testsub")
        self.assertEqual(d.author, "bob")
        self.assertEqual(d.score, 7)
        self.assertEqual(d.thread_url, "https://www.reddit.com/r/testsub/comments/p1/")

    def test_comment_without_permalink_gets_fallback(self):
        c = dict(COM)
        del c["permalink"]
        d = _c()._comment_doc(c, "testsub")
        self.assertEqual(d.source_url, "https://www.reddit.com/r/testsub/comments/p1/_/c1/")

    def test_deleted_and_short_are_dropped(self):
        self.assertIsNone(_c()._submission_doc({**SUB, "author": "[deleted]"}, "s"))
        self.assertIsNone(_c()._comment_doc({**COM, "body": "[removed]"}, "s"))
        self.assertIsNone(_c()._comment_doc({**COM, "body": "meh"}, "s"))


class ApiSweepTest(unittest.TestCase):
    def _resp(self, data, status=200, headers=None):
        r = mock.Mock()
        r.status_code = status
        r.headers = headers or {}
        r.json.return_value = {"data": data}
        r.text = ""
        return r

    def test_pagination_advances_cursor_and_dedupes(self):
        c = _c(page_size=2)
        pages = [
            self._resp([SUB, {**SUB, "id": "p2", "created_utc": 1700000050}]),
            # Overlap: cursor re-fetches p2 (same-second fuzz); must be deduped by id.
            self._resp([{**SUB, "id": "p2", "created_utc": 1700000050},
                        {**SUB, "id": "p3", "created_utc": 1700000060}]),
            self._resp([]),
        ]
        with mock.patch("pipeline.collectors.arcticshift_collector.requests.get",
                        side_effect=pages) as g:
            ids = [s["id"] for s in c._sweep_api("posts", "testsub", cap=0)]
        self.assertEqual(ids, ["p1", "p2", "p3"])
        self.assertEqual(g.call_count, 3)

    def test_submission_cap_respects_limit(self):
        c = _c(page_size=2)
        with mock.patch("pipeline.collectors.arcticshift_collector.requests.get",
                        return_value=self._resp([SUB, {**SUB, "id": "p2"}])):
            ids = [s["id"] for s in c._sweep_api("posts", "testsub", cap=1)]
        self.assertEqual(ids, ["p1"])

    def test_429_backs_off_then_succeeds(self):
        c = _c()
        with mock.patch("pipeline.collectors.arcticshift_collector.requests.get",
                        side_effect=[self._resp([], status=429,
                                                headers={"X-RateLimit-Reset": "0.01"}),
                                     self._resp([SUB])]), \
             mock.patch("pipeline.collectors.arcticshift_collector.time.sleep") as slept:
            page = c._get_page("posts", "testsub", 0)
        self.assertEqual(page[0]["id"], "p1")
        self.assertTrue(slept.called)

    def test_same_second_flood_cannot_loop_forever(self):
        """A full page of same-second items whose ids are all seen must force the cursor
        forward instead of re-requesting the same page for eternity."""
        c = _c(page_size=1)
        same = {**SUB, "id": "p1", "created_utc": 1700000000}
        pages = [self._resp([same]), self._resp([same]), self._resp([])]
        with mock.patch("pipeline.collectors.arcticshift_collector.requests.get",
                        side_effect=pages) as g:
            ids = [s["id"] for s in c._sweep_api("posts", "testsub", cap=0)]
        self.assertEqual(ids, ["p1"])
        self.assertEqual(g.call_count, 3)  # terminated, no infinite loop


class FileModeTest(unittest.TestCase):
    def test_zst_dump_streams_and_maps(self):
        import zstandard
        with tempfile.TemporaryDirectory() as tmp:
            posts = Path(tmp) / "testsub_submissions.zst"
            lines = (json.dumps(SUB) + "\n" + json.dumps({**SUB, "id": "p2"}) + "\n").encode()
            posts.write_bytes(zstandard.ZstdCompressor().compress(lines))
            comments = Path(tmp) / "testsub_comments.jsonl"
            comments.write_text(json.dumps(COM) + "\n", encoding="utf-8")
            c = _c(dump_dir=tmp)
            docs = list(c.collect("https://www.reddit.com/r/testsub/", limit=10))
        kinds = [(d.title != "", d.author) for d in docs]
        self.assertEqual(len(docs), 3)
        self.assertEqual(kinds, [(True, "alice"), (True, "alice"), (False, "bob")])

    def test_file_mode_wins_over_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "testsub_submissions.jsonl").write_text(
                json.dumps(SUB) + "\n", encoding="utf-8")
            c = _c(dump_dir=tmp)
            with mock.patch("pipeline.collectors.arcticshift_collector.requests.get") as g:
                docs = list(c.collect("https://www.reddit.com/r/testsub/", limit=10))
            g.assert_not_called()
        self.assertEqual(len(docs), 1)


class RoutingTest(unittest.TestCase):
    CFG = {"collection": {"arctic_shift": {"enabled": True},
                          "reddit": {"min_comment_len": 20}}}

    def test_historical_reddit_routes_to_arctic_shift(self):
        collector, kind = pick_collector("https://www.reddit.com/r/testsub/", self.CFG,
                                         corpus_mode="historical")
        self.assertEqual(kind, "arctic-shift")
        self.assertIsInstance(collector, ArcticShiftCollector)

    def test_refresh_keeps_live_crawl(self):
        _, kind = pick_collector("https://www.reddit.com/r/testsub/", self.CFG,
                                 corpus_mode="refresh")
        self.assertEqual(kind, "reddit")

    def test_disabled_keeps_live_crawl_even_historical(self):
        cfg = {"collection": {"arctic_shift": {"enabled": False}}}
        _, kind = pick_collector("https://www.reddit.com/r/testsub/", cfg,
                                 corpus_mode="historical")
        self.assertEqual(kind, "reddit")


if __name__ == "__main__":
    unittest.main()
