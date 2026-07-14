import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.orchestrate import pick_collector


def _cfg():
    return {
        "collection": {
            "reddit": {},
        }
    }


class PickCollectorTests(unittest.TestCase):
    def test_reddit_refresh_revisits_known_threads(self):
        collector, kind = pick_collector(
            "https://www.reddit.com/r/python/",
            _cfg(),
            known_thread_urls={"https://www.reddit.com/r/python/comments/abc123/example/"},
            corpus_mode="refresh",
        )

        self.assertEqual(kind, "reddit")
        self.assertEqual(collector.sort, "new")
        self.assertEqual(collector.skip_thread_urls, set())
        self.assertEqual(collector.sort_plan, [("new", None), ("hot", None), ("top", "year")])

    def test_reddit_backfill_still_skips_known_threads(self):
        known = {"https://www.reddit.com/r/python/comments/abc123/example/"}
        collector, kind = pick_collector(
            "https://www.reddit.com/r/python/",
            _cfg(),
            known_thread_urls=known,
            corpus_mode="backfill",
        )

        self.assertEqual(kind, "reddit")
        self.assertEqual(collector.sort, "new")
        self.assertTrue(collector.skip_thread_urls)
        self.assertEqual(
            collector.sort_plan,
            [("new", None), ("top", "all"), ("top", "year"), ("controversial", "all"), ("hot", None)],
        )

    def test_reddit_seed_top_hour_leads_refresh_cycle(self):
        collector, kind = pick_collector(
            "https://www.reddit.com/r/python/top/?t=hour",
            _cfg(),
            known_thread_urls={"https://www.reddit.com/r/python/comments/abc123/example/"},
            corpus_mode="refresh",
        )

        self.assertEqual(kind, "reddit")
        self.assertEqual(collector.sort_plan[0], ("top", "hour"))
        self.assertIn(("new", None), collector.sort_plan)
        self.assertIn(("hot", None), collector.sort_plan)

    def test_reddit_historical_mode_uses_backfill_plan(self):
        collector, kind = pick_collector(
            "https://www.reddit.com/r/python/",
            _cfg(),
            known_thread_urls={"https://www.reddit.com/r/python/comments/abc123/example/"},
            corpus_mode="historical",
            extra_thread_urls=["https://www.reddit.com/r/python/comments/zzz999/sample/"],
        )

        self.assertEqual(kind, "reddit")
        self.assertEqual(collector.sort_plan[0], ("new", None))
        self.assertIn(("top", "all"), collector.sort_plan)
        self.assertIn(("controversial", "all"), collector.sort_plan)
        self.assertTrue(collector.skip_thread_urls)
        self.assertTrue(collector.extra_thread_urls)


if __name__ == "__main__":
    unittest.main()
