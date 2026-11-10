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

At a 0.5 share, "spamming issues" (0.29), "writing is difficult to read" (0.17) and
"frustration with baseless accusations and derailment of topics" (0.00) still ranked 6, 17
and 11. The bar is now 0.3, the vocabulary names posts, threads, OPs and formatting, and
`argument_chatter` catches posters insulting each other, which names no venue at all.
"""
from __future__ import annotations

import unittest

from pipeline.s7_filters import evaluate_cluster, platform_meta_share, share_tag_share
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

    def test_a_third_of_the_members_is_enough(self):
        """"spamming issues" escaped the old majority bar at 0.29."""
        third = _cluster([META, MARKET, MARKET])
        self.assertEqual(evaluate_cluster(third, ["platform_meta"]), ["platform_meta"])
        quarter = _cluster([META, MARKET, MARKET, MARKET])
        self.assertEqual(evaluate_cluster(quarter, ["platform_meta"]), [])

    def test_a_pure_market_theme_is_never_flagged(self):
        self.assertEqual(evaluate_cluster(_cluster([MARKET] * 6), ["platform_meta"]), [])

    def test_an_empty_theme_does_not_divide_by_zero(self):
        self.assertEqual(platform_meta_share({"pains": []}), 0.0)


BOTH = ["platform_meta", "argument_chatter"]
SPAM = ["frequent unwanted posts on home page", "can we stop the endless spamming?",
        "every post here reads like a bot wrote it"]
UNREADABLE = ["you write like a wall of text with no punctuation",
              "I can't follow this at all", "learn to use paragraphs please"]
ARGUMENT = ["Don\u2019t make assumptions dumbass",
            "You're wasting our time and yours. Don't bother.",
            "Stop assuming the worst, your kind never listens",
            "You obviously have no idea what you are talking about",
            "that is simply not true"]
DATING = ["Tinder passport mode gives me no matches",
          "since post-covid the apps here are dead",
          "rent is sub $700 but nobody on tinder replies",
          "passport mode just shows me to bots",
          "I paid for gold and still got zero likes in Bangkok"]


class VenueChatterTests(unittest.TestCase):
    """Labels that ranked in pbfamily0001's top 25, rebuilt from their members' own words."""

    def test_spam_and_home_page_complaints_are_venue_chatter(self):
        self.assertEqual(evaluate_cluster(_cluster(SPAM), BOTH), ["platform_meta"])

    def test_complaints_about_how_posts_are_written_are_venue_chatter(self):
        self.assertEqual(evaluate_cluster(_cluster(UNREADABLE), BOTH), ["platform_meta"])

    def test_posters_insulting_each_other_are_argument_chatter(self):
        cluster = _cluster(ARGUMENT)
        self.assertEqual(share_tag_share(cluster, "argument_chatter"), 0.8)
        self.assertEqual(evaluate_cluster(cluster, BOTH), ["argument_chatter"])

    def test_lol_is_a_jab_only_when_aimed_at_someone(self):
        self.assertEqual(share_tag_share(_cluster(["lol the flight got cancelled again"]),
                                         "argument_chatter"), 0.0)
        self.assertEqual(share_tag_share(_cluster(["lol youre so triggered"]),
                                         "argument_chatter"), 1.0)

    def test_a_curly_apostrophe_is_still_an_apostrophe(self):
        self.assertEqual(share_tag_share(_cluster(["You\u2019re lying and you know it"]),
                                         "argument_chatter"), 1.0)

    def test_a_dating_app_complaint_is_not_venue_chatter(self):
        """"post-covid" is a date and "sub $700" a price; neither is the forum."""
        self.assertEqual(evaluate_cluster(_cluster(DATING), BOTH), [])

    def test_argument_chatter_is_silent_unless_enabled(self):
        cluster = _cluster(ARGUMENT)
        self.assertEqual(evaluate_cluster(cluster, ["platform_meta"]), [])
        self.assertEqual(evaluate_cluster(cluster, []), [])


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
