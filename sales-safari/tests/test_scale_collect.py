"""Tests for collection scaling: per-domain kickoff lanes + Discourse delta refresh.

Politeness invariant pinned here: parallelism NEVER crosses a domain boundary - a lane
holds every source of one domain, in submission order, so reddit.com sees exactly the
same request pattern as the old fully-serial queue.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.collectors.base import Document
from pipeline.collectors.discourse_collector import DiscourseCollector
from pipeline.store import Store
from webapp.app import _lane_key, _lane_plan


class LanePlanTest(unittest.TestCase):
    def test_mirror_hosts_share_a_lane(self):
        self.assertEqual(_lane_key("https://www.reddit.com/r/a/"),
                         _lane_key("https://old.reddit.com/r/b/"))

    def test_distinct_domains_get_distinct_lanes(self):
        sources = [
            {"id": "1", "url": "https://www.reddit.com/r/a/"},
            {"id": "2", "url": "https://forum.example.com/c/pains"},
            {"id": "3", "url": "https://www.reddit.com/r/b/"},
            {"id": "4", "url": "https://other.example.org/f/"},
        ]
        lanes = _lane_plan(sources)
        self.assertEqual(len(lanes), 3)
        reddit_lane = next(l for l in lanes if "reddit" in l[0][1]["url"])
        # Same-domain sources stay in ONE lane, original order preserved - this is the
        # politeness guarantee.
        self.assertEqual([src["id"] for _, src in reddit_lane], ["1", "3"])
        # Original indices survive so kickoff items stay aligned.
        self.assertEqual([i for i, _ in reddit_lane], [0, 2])

    def test_single_domain_collapses_to_serial(self):
        sources = [{"id": str(n), "url": f"https://www.reddit.com/r/s{n}/"} for n in range(4)]
        self.assertEqual(len(_lane_plan(sources)), 1)


def _collector(**kw):
    kw.setdefault("max_posts_per_thread", 200)
    return DiscourseCollector(**kw)


class DiscourseDeltaTest(unittest.TestCase):
    STATS = {
        # topic 11: we hold 5 posts, newest 2026-01-10. 12: capped at 200.
        "https://f.test/t/slug-a/11": {"count": 5, "max_created_at": "2026-01-10T00:00:00.000Z"},
        "https://f.test/t/slug-b/12": {"count": 200, "max_created_at": "2026-01-01T00:00:00.000Z"},
    }

    def test_unchanged_topic_skips(self):
        c = _collector(known_thread_stats=self.STATS)
        self.assertTrue(c._topic_is_unchanged(
            {"id": 11, "last_posted_at": "2026-01-10T00:00:00.000Z"}))
        self.assertTrue(c._topic_is_unchanged(
            {"id": 11, "last_posted_at": "2026-01-09T12:00:00.000Z"}))

    def test_new_upstream_post_fetches(self):
        c = _collector(known_thread_stats=self.STATS)
        self.assertFalse(c._topic_is_unchanged(
            {"id": 11, "last_posted_at": "2026-01-11T08:00:00.000Z"}))

    def test_capped_topic_always_skips(self):
        """stream[:cap] yields a topic's OLDEST ids; growth past the cap can never
        produce new docs, so re-fetching a capped megathread is pure waste."""
        c = _collector(known_thread_stats=self.STATS)
        self.assertTrue(c._topic_is_unchanged(
            {"id": 12, "last_posted_at": "2026-06-01T00:00:00.000Z"}))

    def test_unknown_topic_fetches(self):
        c = _collector(known_thread_stats=self.STATS)
        self.assertFalse(c._topic_is_unchanged(
            {"id": 99, "last_posted_at": "2020-01-01T00:00:00.000Z"}))

    def test_no_stats_means_no_delta_behavior(self):
        self.assertFalse(_collector()._topic_is_unchanged(
            {"id": 11, "last_posted_at": "2020-01-01T00:00:00.000Z"}))

    def test_collect_fetches_only_changed_topics(self):
        c = _collector(known_thread_stats=self.STATS)
        topics = [
            {"id": 11, "title": "quiet", "last_posted_at": "2026-01-10T00:00:00.000Z"},
            {"id": 13, "title": "active", "last_posted_at": "2026-02-01T00:00:00.000Z"},
        ]
        fetched = []
        with mock.patch.object(DiscourseCollector, "_category_topics", return_value=topics), \
             mock.patch.object(DiscourseCollector, "_thread_posts",
                               side_effect=lambda base, tid: fetched.append(tid) or iter(())):
            list(c.collect("https://f.test/c/all", limit=10))
        self.assertEqual(fetched, [13])

    def test_direct_thread_seed_ignores_delta(self):
        """An explicit /t/ seed is a user request for THAT thread - always fetch."""
        c = _collector(known_thread_stats=self.STATS)
        fetched = []
        with mock.patch.object(DiscourseCollector, "_thread_posts",
                               side_effect=lambda base, tid: fetched.append(tid) or iter(())):
            list(c.collect("https://f.test/t/slug-a/11", limit=10))
        self.assertEqual(fetched, [11])


class CorpusThreadStatsTest(unittest.TestCase):
    def test_stats_count_and_newest_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "s.sqlite"))
            store.start_run("r1", "https://f.test/c/all", use_corpus=True)
            for n, ts in ((1, "2026-01-01T00:00:00.000Z"), (2, "2026-01-10T00:00:00.000Z")):
                d = Document(
                    source_type="forum",
                    source_url=f"https://f.test/t/slug-a/11/{n}",
                    permalink=f"https://f.test/t/slug-a/11/{n}",
                    title="a", raw_markdown="body text long enough", author=f"u{n}",
                    thread_url="https://f.test/t/slug-a/11", created_at=ts,
                    source_granularity="post")
                store.upsert_document("r1", d)
                did = store.get_document_id_by_source_url(d.source_url)
                store.link_document_to_corpus("f.test:c/all", did, d.fetched_at)
            stats = store.get_corpus_thread_stats("f.test:c/all")
            st = stats["https://f.test/t/slug-a/11"]
            self.assertEqual(st["count"], 2)
            self.assertEqual(st["max_created_at"], "2026-01-10T00:00:00.000Z")
            store.close()


if __name__ == "__main__":
    unittest.main()
