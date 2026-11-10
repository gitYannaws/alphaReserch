"""Stage 7b asks a buyer test, and stage 9 stops letting age and a blind verdict decide rank.

Run pbfamily0001: 7b asked a 14B model "could software solve it" on six pains x 240 chars and
got `yes 0.80 "Software can manage emotions and responses."` for "frustration with people".
`rank.allowed_solvability` made that verdict a hard gate, the stored confidence was never
read, and raw persistence (1-10) multiplied rank unbounded: it was the strongest rank driver
(Spearman +0.62), ahead of demand itself.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from pipeline import s7b_softfilter
from pipeline.s9_rank import rank_run
from pipeline.store import Store

RUN = "run1"


class _TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class BuyerTestVerdicts(_TempStore):
    def _theme(self, cid, n_pains=3, span="I rebuild the same invoice by hand every month"):
        pids = []
        for i in range(n_pains):
            self.store.insert_pain(RUN, {"document_id": f"{cid}-{i}", "author_hash": f"a{i}",
                                         "verbatim_span": f"{span} ({i})",
                                         "complaint": f"manual invoicing {i}"})
            pids.append(self.store.conn.execute(
                "SELECT id FROM pains WHERE document_id=?", (f"{cid}-{i}",)).fetchone()[0])
        self.store.save_cluster(cid, RUN, f"theme {cid}", n_pains, n_pains, pids)

    def _classify(self, verdicts):
        prompts = []

        def fake(prompt, cfg):
            prompts.append(prompt)
            return json.dumps(verdicts), "test"

        original = s7b_softfilter._call_extractor
        s7b_softfilter._call_extractor = fake
        try:
            s7b_softfilter.softfilter_run(self.store, RUN, {}, min_support=None)
        finally:
            s7b_softfilter._call_extractor = original
        rows = self.store.conn.execute(
            "SELECT cluster_id, solvable, evidence, warnings FROM soft_filters WHERE run_id=?",
            (RUN,)).fetchall()
        return {r[0]: {"solvable": r[1], "evidence": json.loads(r[2]) if r[2] else None,
                       "warnings": json.loads(r[3])} for r in rows}, prompts

    def test_yes_on_one_demand_signal_is_downgraded_to_partial(self):
        """"false" as a string is truthy in Python; it must not count as evidence."""
        self._theme("c1")
        rows, _ = self._classify([{"id": "c1", "solvable": "yes", "confidence": 0.8,
                                   "reason": "software can manage it",
                                   "evidence": {"workaround": "true", "cost": "false",
                                                "recurring": "false", "payer": False}}])
        self.assertEqual(rows["c1"]["solvable"], "partial")
        self.assertEqual(rows["c1"]["evidence"], {
            "workaround": True, "cost": False, "recurring": False, "payer": False,
            "platform_only": False, "venue": False})

    def test_yes_on_two_demand_signals_stands(self):
        self._theme("c1")
        rows, _ = self._classify([{"id": "c1", "solvable": "yes", "confidence": 0.8,
                                   "reason": "monthly invoicing",
                                   "evidence": {"cost": True, "recurring": True}}])
        self.assertEqual(rows["c1"]["solvable"], "yes")

    def test_a_fix_only_the_platform_can_ship_is_a_platform_feature(self):
        self._theme("c1")
        rows, _ = self._classify([{"id": "c1", "solvable": "yes", "confidence": 0.9,
                                   "reason": "Tinder should add it",
                                   "evidence": {"workaround": True, "cost": True,
                                                "recurring": True, "platform_only": True}}])
        self.assertEqual(rows["c1"]["solvable"], "platform_feature")

    def test_venue_is_recorded_as_evidence_and_not_as_a_warning(self):
        self._theme("c1")
        rows, _ = self._classify([{"id": "c1", "solvable": "no", "confidence": 0.9,
                                   "reason": "complaint about moderators",
                                   "evidence": {"venue": True}}])
        self.assertTrue(rows["c1"]["evidence"]["venue"])
        self.assertEqual(rows["c1"]["warnings"], [], "warning tags belong to s7_filters")

    def test_the_judge_sees_the_verbatim_words_and_the_theme_size(self):
        long_span = "x" * 290 + " MUST-NOT-APPEAR"
        self._theme("c1", n_pains=14, span=long_span)
        _, prompts = self._classify([])
        payload = json.loads(prompts[0].split("THEMES:\n", 1)[1])[0]
        self.assertEqual(payload["size"], 14)
        self.assertEqual(payload["distinct_authors"], 14)
        self.assertEqual(len(payload["pains"]), 12)
        for text in payload["pains"]:
            self.assertTrue(text.startswith("x" * 290), "the poster's own words come first")
            self.assertNotIn("MUST-NOT-APPEAR", text, "the span is capped at 300 chars")
            self.assertIn("manual invoicing", text, "the summary follows the span")


class RankFormula(_TempStore):
    def _theme(self, cid, demand, persistence, solvable="yes", confidence=0.9):
        self.store.save_cluster(cid, RUN, f"theme {cid}", 6, 5, [])
        self.store.save_demand_score(RUN, cid, {
            "pain_intensity": 5, "frequency": 5, "willingness_to_pay": 5, "reachability": 5,
            "demand_score": demand, "persistence_score": persistence,
            "evidence_count": 6, "distinct_authors": 5})
        self.store.save_soft_filter(RUN, cid, solvable, confidence, "", [])

    def _rank(self, allowed=("yes", "partial")):
        stats = rank_run(self.store, RUN, allowed_solvability=None if allowed is None else list(allowed))
        rows = self.store.get_ranked_clusters(RUN, include_dropped=True)
        return stats, {r["cluster_id"]: r for r in rows}

    def test_persistence_is_a_bounded_multiplier(self):
        """Demand 7.5, not 7: the bounded multipliers are 1.222 and 0.861, a 1.42x spread,
        so a 7-vs-5 demand gap (1.40x) still loses to persistence 9.5 by a hair."""
        self._theme("old_and_weak", demand=5.0, persistence=9.5)
        self._theme("young_and_strong", demand=7.5, persistence=3.0)
        _, rows = self._rank()
        self.assertEqual(rows["young_and_strong"]["rank"], 1)
        self.assertEqual(rows["old_and_weak"]["rank"], 2)

        old = rows["old_and_weak"]["rank_breakdown"]
        self.assertEqual(old["persistence"], 9.5, "raw persistence is kept for the reader")
        self.assertAlmostEqual(old["persistence_multiplier"], 0.75 + 0.5 * 8.5 / 9)
        self.assertAlmostEqual(
            rows["young_and_strong"]["rank_breakdown"]["persistence_multiplier"],
            0.75 + 0.5 * 2 / 9)
        for row in rows.values():
            b = row["rank_breakdown"]
            self.assertEqual(row["rank_score"],
                             round(b["demand"] * b["persistence_multiplier"] * b["solvable_weight"], 2),
                             "the stored breakdown must reproduce the score")

    def test_the_judges_confidence_moves_the_rank(self):
        self._theme("sure", demand=6.0, persistence=5.0, confidence=0.9)
        self._theme("unsure", demand=6.0, persistence=5.0, confidence=0.2)
        _, rows = self._rank()
        self.assertGreater(rows["sure"]["rank_score"], rows["unsure"]["rank_score"])
        self.assertEqual(rows["sure"]["rank"], 1)
        self.assertAlmostEqual(rows["sure"]["solvable_weight"], 0.95)
        self.assertAlmostEqual(rows["unsure"]["solvable_weight"], 0.6)

    def test_a_platform_feature_is_never_ranked(self):
        for allowed in (("yes", "partial"), None):
            with self.subTest(allowed=allowed):
                self._theme("tinder", demand=9.0, persistence=5.0, solvable="platform_feature")
                self._theme("tool", demand=4.0, persistence=5.0)
                _, rows = self._rank(allowed)
                self.assertTrue(rows["tinder"]["dropped"])
                self.assertIn("platform_feature", rows["tinder"]["filter_reasons"])
                self.assertNotIn("not_rankable_solvability", rows["tinder"]["filter_reasons"])
                self.assertEqual(rows["tool"]["rank"], 1)

    def test_a_platform_feature_verdict_keeps_the_gate_on(self):
        """It is a real verdict, so a run holding only it still has a working 7b."""
        self._theme("tinder", demand=9.0, persistence=5.0, solvable="platform_feature")
        self._theme("unasked", demand=8.0, persistence=5.0, solvable="unknown", confidence=0.0)
        stats, rows = self._rank()
        self.assertEqual(stats["solvability_gate"], "applied")
        self.assertIn("not_rankable_solvability", rows["unasked"]["filter_reasons"])

    def test_no_verdicts_at_all_still_ranks_everything(self):
        """A failed 7b degrades the ranking, never empties it (run 9af5b27db46e: ranked=0)."""
        self._theme("a", demand=6.0, persistence=5.0, solvable="unknown", confidence=0.0)
        self._theme("b", demand=4.0, persistence=5.0, solvable="unknown", confidence=0.0)
        stats, rows = self._rank()
        self.assertEqual(stats["solvability_gate"], "disabled_no_verdicts")
        self.assertEqual((rows["a"]["rank"], rows["b"]["rank"]), (1, 2))
        self.assertEqual(rows["a"]["rank_breakdown"]["confidence"], 0.5,
                         "no verdict means no confidence; the stored 0.0 must not count")


if __name__ == "__main__":
    unittest.main()
