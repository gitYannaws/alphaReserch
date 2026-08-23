"""People on Reddit complain about Reddit, and that is not this market's pain.

Run pbfamily0001 ranked "moderator abuse and unfair bans" at 8, "subreddit toxicity" at 11
and "Posts receive very little engagement" at 12; the re-run put "frequent unwanted posts on
home page" at 4 and "Reddit showing disgusting content" at 10. Real complaints, wrong market.

Two rules are under test, and they are deliberately different from the other warning tags:

- The tag is judged per MEMBER and then by SHARE. Every other tag fires on a single regex hit
  anywhere in the theme's concatenated text, which is right for a caveat ("someone mentioned
  a visa") and wrong here: one post saying "this sub" must not brand a whole market theme as
  venue chatter, because this tag can be configured to DROP.
- Warnings stay advisory BY DEFAULT. `rank.drop_warnings` is the operator naming, in config,
  the few that disqualify. With it empty, nothing changes.
"""
from __future__ import annotations

import unittest

from pipeline.s7_filters import (PLATFORM_META_MIN_SHARE, evaluate_cluster,
                                 platform_meta_share)
from pipeline.s9_rank import rank_run

META = "the mods on this subreddit keep deleting my posts"
MARKET = "wire transfer fees abroad are brutal every single month"


def _cluster(texts):
    return {"id": "c1", "label": "t", "size": len(texts), "distinct_authors": len(texts),
            "pains": [{"complaint": t, "workflow_pain": "", "workaround": "", "wish": "",
                       "verbatim_span": t} for t in texts]}


class ShareRuleTests(unittest.TestCase):
    def test_a_theme_that_is_all_venue_chatter_is_flagged(self):
        cluster = _cluster([META] * 5)
        self.assertEqual(platform_meta_share(cluster), 1.0)
        self.assertEqual(evaluate_cluster(cluster, ["platform_meta"]), ["platform_meta"])

    def test_one_stray_mention_does_not_brand_a_market_theme(self):
        """The failure this rule exists to prevent: dropping a real theme over one post."""
        cluster = _cluster([MARKET, MARKET, MARKET, MARKET, META])
        self.assertEqual(platform_meta_share(cluster), 0.2)
        self.assertEqual(evaluate_cluster(cluster, ["platform_meta"]), [])

    def test_the_threshold_is_a_majority(self):
        self.assertGreaterEqual(PLATFORM_META_MIN_SHARE, 0.5)
        half = _cluster([META, META, MARKET, MARKET])
        self.assertEqual(evaluate_cluster(half, ["platform_meta"]), ["platform_meta"])

    def test_a_pure_market_theme_is_never_flagged(self):
        self.assertEqual(evaluate_cluster(_cluster([MARKET] * 6), ["platform_meta"]), [])

    def test_an_empty_theme_does_not_divide_by_zero(self):
        self.assertEqual(platform_meta_share({"pains": []}), 0.0)


class _RankStore:
    """Two themes with identical support; only the warning tag differs."""

    def __init__(self, warnings):
        self.saved = []
        self.conn = self
        self._warnings = warnings

    def execute(self, sql, params):
        import json
        return type("R", (), {"fetchall": lambda _s: [
            ("c1", 6.0, 3.0, 0.0, 0, json.dumps(self._warnings), "yes"),
        ]})()

    def get_cluster_details(self, run_id, cluster_ids=None):
        return [{"id": "c1", "label": "t", "size": 6, "distinct_authors": 5,
                 "pains": [{"thread_url": "https://example.test/t%d" % (i % 3)}
                           for i in range(6)]}]

    def clear_rankings(self, run_id):
        pass

    def save_ranking(self, run_id, row):
        self.saved.append(row)

    def set_stage(self, run_id, stage, status):
        pass


SUPPORT = {"evidence_count": 5, "distinct_authors": 4, "distinct_threads": 3}


class DropWarningsTests(unittest.TestCase):
    def _run(self, warnings, drop):
        store = _RankStore(warnings)
        stats = rank_run(store, "run", min_support=SUPPORT,
                         allowed_solvability=["yes", "partial"], drop_warnings=drop)
        return stats, store.saved[0]

    def test_warnings_stay_advisory_by_default(self):
        """The existing design rule: a caveat must never quietly delete a theme."""
        stats, row = self._run(["platform_meta"], None)
        self.assertEqual(stats["ranked"], 1)
        self.assertFalse(row["dropped"])
        self.assertIn("platform_meta", row["filter_reasons"], "the caveat is still shown")

    def test_a_named_warning_disqualifies(self):
        stats, row = self._run(["platform_meta"], ["platform_meta"])
        self.assertEqual(stats["ranked"], 0)
        self.assertEqual(stats["dropped_by_warning"], 1)
        self.assertTrue(row["dropped"])
        self.assertEqual(row["rank_breakdown"]["disqualifying_warnings"], ["platform_meta"])

    def test_an_unnamed_warning_still_does_not_drop(self):
        stats, row = self._run(["language_barrier"], ["platform_meta"])
        self.assertEqual(stats["ranked"], 1)
        self.assertEqual(stats["dropped_by_warning"], 0)
        self.assertFalse(row["dropped"])

    def test_a_clean_theme_is_unaffected(self):
        stats, _row = self._run([], ["platform_meta"])
        self.assertEqual(stats["ranked"], 1)


if __name__ == "__main__":
    unittest.main()
