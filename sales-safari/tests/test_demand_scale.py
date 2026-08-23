"""Size and time must be VISIBLE in the score.

Three defects, all measured on run pbfamily0001 and all the same shape: a signal was
computed and then thrown away by a ceiling or a proxy.

1. `frequency` was `authors * 2.5` under a hard min(10, ...), so it maxed out at FOUR
   authors. It read exactly 10.00 on 40% of all themes and on effectively every theme that
   ranked, making a 5-author theme and a 23-author theme identical. The upvote signal was
   added inside the same clamp and vanished with it, on a corpus where 177,130 documents
   carry a score of up to 2,990.
2. `recurrence` led with `authors * 1.8` (ceiling at six), so it was a second noisy copy of
   frequency rather than a measure of the pain coming back.
3. `persistence` counted words like "still" and "again". 7,775 of 8,925 themes got the exact
   same 3.0 and the whole run held 4 distinct values -- a rank multiplier that does nothing.
   Real dates sat unused on 179,533 documents spanning four years.

After: frequency and recurrence pin at 10 on ZERO themes, persistence has 686 distinct
values across 1.0-9.53, and the median size of the top 20 rose from 6 to 9.
"""
from __future__ import annotations

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline.s6_demand import (_log_scale, _persistence, corpus_latest_date, score_cluster)

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = {"pain_intensity": 3, "frequency": 3, "willingness_to_pay": 3,
           "reachability": 2, "recurrence_score": 3}
NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _cluster(authors=5, pains=None, threads=1, days_ago=None, score=None):
    """A theme with `authors` distinct authors, one pain each unless `pains` says otherwise."""
    n = pains if pains is not None else authors
    days_ago = days_ago if days_ago is not None else [10] * n
    return {
        "id": "c1",
        "label": "wire fees",
        "size": n,
        "distinct_authors": authors,
        "pains": [{
            "complaint": "wire fees are painful",
            "workflow_pain": "", "workaround": "", "wish": "", "persona": "",
            "verbatim_span": "wire fees are painful",
            "source_permalink": "https://example.test/%d" % i,
            "thread_url": "https://example.test/t%d" % (i % threads),
            "source_granularity": "post",
            "score": score,
            "created_at": (NOW - timedelta(days=days_ago[i % len(days_ago)])).isoformat(),
        } for i in range(n)],
    }


class LogScaleTests(unittest.TestCase):
    def test_it_reaches_the_cap_exactly_at_the_anchor(self):
        self.assertAlmostEqual(_log_scale(25, 25), 10.0, places=6)

    def test_it_never_exceeds_the_cap(self):
        self.assertEqual(_log_scale(10_000, 25), 10.0)

    def test_it_keeps_rising_past_the_old_linear_ceiling(self):
        """The whole point: more evidence must always mean more score."""
        prev = -1.0
        for value in (1, 2, 4, 6, 10, 15, 20, 24):
            score = _log_scale(value, 25)
            self.assertGreater(score, prev, f"score went flat at {value}")
            prev = score

    def test_zero_and_negative_are_zero(self):
        self.assertEqual(_log_scale(0, 25), 0.0)
        self.assertEqual(_log_scale(-3, 25), 0.0)


class FrequencySeesSizeTests(unittest.TestCase):
    def _freq(self, **kw):
        return score_cluster(_cluster(**kw), WEIGHTS, NOW)["frequency"]

    def test_more_authors_always_scores_higher(self):
        """4 authors used to be the ceiling; 5 and 23 were the same number."""
        four, five, twentythree = self._freq(authors=4), self._freq(authors=5), self._freq(authors=23)
        self.assertLess(four, five)
        self.assertLess(five, twentythree)

    def test_a_big_theme_does_not_pin_at_ten(self):
        self.assertLess(self._freq(authors=23), 10.0)

    def test_upvotes_still_move_the_score(self):
        """The endorsement bonus used to be added inside the clamp and disappear."""
        quiet = self._freq(authors=6, score=0)
        loud = self._freq(authors=6, score=2990)
        self.assertGreater(loud, quiet, "a 2,990-upvote theme must outscore a silent one")


class RecurrenceIsNotFrequencyTests(unittest.TestCase):
    def _rec(self, **kw):
        return score_cluster(_cluster(**kw), WEIGHTS, NOW)["recurrence_score"]

    def test_author_count_alone_does_not_raise_recurrence(self):
        """Recurrence is about the pain coming back, not about how many people showed up."""
        self.assertEqual(self._rec(authors=5, threads=1), self._rec(authors=20, threads=1))

    def test_spreading_across_threads_does_raise_it(self):
        self.assertLess(self._rec(authors=8, threads=1), self._rec(authors=8, threads=6))


class PersistenceFromDatesTests(unittest.TestCase):
    def _pers(self, days_ago, pains=None):
        cluster = _cluster(authors=5, pains=pains or len(days_ago), days_ago=days_ago)
        return _persistence(cluster, "", NOW, {"months": 36})

    def test_a_long_lived_and_still_live_pain_scores_high(self):
        score, ev = self._pers([1200, 900, 600, 300, 20, 5])
        self.assertEqual(ev["method"], "dates")
        self.assertGreater(score, 7.5)

    def test_a_single_recent_burst_is_not_persistence(self):
        burst, _ = self._pers([9, 8, 7, 6, 5])
        long_lived, _ = self._pers([1200, 900, 600, 300, 20, 5])
        self.assertLess(burst, long_lived)

    def test_an_old_dead_pain_scores_below_a_live_one(self):
        dead, _ = self._pers([1400, 1300, 1250, 1200])
        live, _ = self._pers([1400, 1300, 1250, 20])
        self.assertLess(dead, live)

    def test_it_produces_a_real_spread_not_one_repeated_number(self):
        scores = {self._pers(d)[0] for d in ([5, 6], [200, 5], [800, 5], [1300, 900, 5],
                                             [1300, 1200], [60, 30, 5])}
        self.assertGreaterEqual(len(scores), 5, "the old version had 4 values for 8,925 themes")

    def test_no_dates_falls_back_to_the_keyword_proxy(self):
        cluster = _cluster(authors=5)
        for pain in cluster["pains"]:
            pain["created_at"] = None
        score, ev = _persistence(cluster, "it still happens again every time", None, {})
        self.assertEqual(ev["method"], "keyword_fallback")
        self.assertGreater(score, 3.0)

    def test_now_comes_from_the_corpus_not_the_wall_clock(self):
        """Re-scoring a finished run tomorrow must not quietly age its themes."""
        cluster = _cluster(authors=4, days_ago=[100, 80, 60, 10])
        latest = corpus_latest_date([cluster])
        self.assertIsNotNone(latest)
        first, _ = _persistence(cluster, "", latest, {"months": 36})
        second, _ = _persistence(cluster, "", latest, {"months": 36})
        self.assertEqual(first, second)
        aged, _ = _persistence(cluster, "", latest + timedelta(days=400), {"months": 36})
        self.assertLess(aged, first, "a later 'now' should read as less live, if we used it")


class EveryCallSitePassesTheKnobs(unittest.TestCase):
    """demand_run and rank_run both grew a parameter. A caller that forgets it silently
    reverts to the old behaviour, which is exactly how webapp/app.py once ranked every GUI
    run without the solvability gate."""

    SITES = (ROOT / "pipeline" / "resume.py", ROOT / "pipeline" / "analyze.py",
             ROOT / "webapp" / "app.py")

    def _kwargs(self, path, func):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                    getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func:
                out.append({k.arg for k in node.keywords})
        return out

    def test_demand_run_always_gets_full_at(self):
        for path in self.SITES:
            for kwargs in self._kwargs(path, "demand_run"):
                with self.subTest(path=path.name):
                    self.assertIn("full_at", kwargs)

    def test_rank_run_always_gets_drop_warnings(self):
        for path in self.SITES:
            for kwargs in self._kwargs(path, "rank_run"):
                with self.subTest(path=path.name):
                    self.assertIn("drop_warnings", kwargs)


if __name__ == "__main__":
    unittest.main()
