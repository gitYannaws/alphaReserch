"""Stage 7b must not pay an LLM to classify themes that can never rank.

On run pbfamily0001 the pass spent 186 minutes classifying all 8,925 themes at ~1.25s each,
and only 2,185 of them could clear rank's own min_support. Restricting the LLM to the
rankable quarter costs about 46 minutes and, checked against that run's stored rankings,
loses NONE of the 856 themes that ranked.

The risk this guards is drift: if 7b's idea of "rankable" ever diverges from stage 9's, a
theme rank would have accepted becomes a silently missing opportunity. Both now call the
same `cluster_support` / `support_reasons` pair, and these tests hold that line.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from pipeline import s7b_softfilter
from pipeline.s9_rank import cluster_support, support_reasons

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = {"evidence_count": 5, "distinct_authors": 4, "distinct_threads": 3}


def _theme(cid, n_pains, n_authors, n_threads):
    return {
        "id": cid,
        "label": f"theme {cid}",
        "size": n_pains,
        "distinct_authors": n_authors,
        "pains": [{"complaint": f"pain {i}", "wish": "", "workflow_pain": "",
                   "thread_url": f"https://example.test/t{i % max(1, n_threads)}"}
                  for i in range(n_pains)],
    }


class _Store:
    def __init__(self, clusters):
        self.clusters = clusters
        self.rows = {}

    def get_cluster_details(self, run_id, cluster_ids=None):
        return self.clusters

    def clear_soft_filters(self, run_id):
        self.rows.clear()

    def save_soft_filter(self, run_id, cluster_id, solvable, conf, reason, warnings):
        self.rows[cluster_id] = {"solvable": solvable, "confidence": conf,
                                 "reason": reason, "warnings": warnings}

    def set_stage(self, run_id, stage, status):
        pass


class PrefilterTests(unittest.TestCase):
    def setUp(self):
        self.big = _theme("rankable", 6, 5, 3)      # clears every threshold
        self.small = _theme("tiny", 2, 2, 1)        # clears none
        self.asked = []

    def _run(self, min_support):
        store = _Store([self.big, self.small])

        def fake(prompt, cfg):
            self.asked.append(prompt)
            import json as _json
            ids = [t["id"] for t in _json.loads(prompt.split("THEMES:\n", 1)[1])]
            return (_json.dumps([{"id": i, "solvable": "yes", "confidence": 0.9,
                                  "reason": "software"} for i in ids]), "test")

        original = s7b_softfilter._call_extractor
        s7b_softfilter._call_extractor = fake
        try:
            return store, s7b_softfilter.softfilter_run(store, "run", {},
                                                        min_support=min_support)
        finally:
            s7b_softfilter._call_extractor = original

    def test_unrankable_theme_is_never_sent_to_the_llm(self):
        store, stats = self._run(SUPPORT)
        self.assertEqual(stats["skipped_unrankable"], 1)
        self.assertEqual(stats["classified"], 1)
        self.assertNotIn("tiny", self.asked[0], "the tiny theme must not reach the prompt")
        self.assertIn("rankable", self.asked[0])

    def test_a_skipped_theme_says_it_was_never_asked(self):
        store, _ = self._run(SUPPORT)
        self.assertEqual(store.rows["tiny"]["solvable"], "not_evaluated")
        self.assertEqual(store.rows["rankable"]["solvable"], "yes")

    def test_every_theme_still_gets_a_row(self):
        store, _ = self._run(SUPPORT)
        self.assertEqual(set(store.rows), {"rankable", "tiny"},
                         "a skipped theme keeps its row so its warnings survive")

    def test_none_classifies_everything(self):
        store, stats = self._run(None)
        self.assertEqual(stats["skipped_unrankable"], 0)
        self.assertEqual(stats["classified"], 2)


class SupportRuleIsShared(unittest.TestCase):
    def test_7b_and_rank_agree_on_what_is_rankable(self):
        for theme, expect_rankable in ((_theme("a", 6, 5, 3), True),
                                       (_theme("b", 5, 4, 3), True),
                                       (_theme("c", 4, 4, 3), False),
                                       (_theme("d", 6, 3, 3), False),
                                       (_theme("e", 6, 5, 1), False)):
            with self.subTest(theme=theme["id"]):
                gaps = support_reasons(cluster_support(theme), SUPPORT)
                self.assertEqual(not gaps, expect_rankable, gaps)

    def test_every_call_site_passes_min_support(self):
        """A caller that forgets it classifies all 8,925 themes again, silently."""
        for path in (ROOT / "pipeline" / "resume.py",
                     ROOT / "pipeline" / "analyze.py",
                     ROOT / "webapp" / "app.py"):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                         and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
                         == "softfilter_run"]
                self.assertTrue(calls, f"no softfilter_run call found in {path.name}")
                for call in calls:
                    self.assertIn("min_support", [k.arg for k in call.keywords],
                                  f"{path.name} calls softfilter_run without min_support")


if __name__ == "__main__":
    unittest.main()
