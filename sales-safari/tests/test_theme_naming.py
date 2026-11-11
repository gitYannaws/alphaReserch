"""Guard the two ways a theme can be given a name that is not true.

Both were found on run pbfamily0001.

1. The semantic audit prompt shows its output shape with a literal example label. A model
   with nothing to say echoes that example instead of naming the theme, and the echo was
   stored: the run's largest cluster (580 members, 493 authors) was titled "specific shared
   customer problem".

2. The stage-12 report counted DROPPED themes as ranked ones, printing "top 50 of 8925
   ranked themes" when 856 of those 8,925 cleared min_support and solvability. It also
   printed a `/ (1 + saturation)` divisor that stage 9 retired on 2026-07-20, so the shown
   arithmetic did not reproduce the rank score printed beside it.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import cluster as cluster_mod
from pipeline.s9_rank import rank_run
from pipeline.s12_report import report_run
from pipeline.store import Store

ROOT = Path(__file__).resolve().parents[1]
RUN = "run1"


class _AuditStore:
    """Only the two members `_semantic_groups` touches."""

    class _Conn:
        def execute(self, sql, params):
            return type("R", (), {"fetchone": lambda self: ("a complaint", "", "", "a span")})()

    conn = _Conn()


class SemanticLabelTests(unittest.TestCase):
    def _groups(self, label):
        raw = (
            '[{"label": %r, "pain_ids": ["p1", "p2", "p3"]}]' % label
        ).replace("'", '"')
        original = cluster_mod._call_extractor
        cluster_mod._call_extractor = lambda prompt, cfg: (raw, "test")
        try:
            return cluster_mod._semantic_groups(
                _AuditStore(), ["p1", "p2", "p3"], {"providers": ["test"]}, 2)
        finally:
            cluster_mod._call_extractor = original

    def test_echoed_placeholder_is_not_accepted_as_a_label(self):
        groups = self._groups("specific shared customer problem")
        self.assertEqual(len(groups), 1, "the group must survive; only its name is bad")
        self.assertEqual(groups[0][1], "", "placeholder must be blanked, not stored")
        self.assertEqual(groups[0][0], ["p1", "p2", "p3"], "no pain may be lost to a bad label")

    def test_a_real_label_still_passes_through(self):
        groups = self._groups("wire fees charged on every transfer")
        self.assertEqual(groups[0][1], "wire fees charged on every transfer")


class ReportHonestyTests(unittest.TestCase):
    """Stage 9 ranks a real Store, then stage 12 reports it: two ranked themes, three dropped."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.sqlite"))
        # Persistence 10 is the multiplier's 1.25 ceiling and confidence 0.9 gives the yes
        # weight 0.95, so the printed factors multiply to the score exactly (4.75).
        themes = [("c0", 4.0, 10.0, "yes"), ("c1", 3.0, 1.0, "partial")] + [
            (f"c{i}", 9.0, 5.0, "platform_feature") for i in (2, 3, 4)]
        for cid, demand, persistence, solvable in themes:
            self.store.save_cluster(cid, RUN, f"theme {cid}", 6, 5, [])
            self.store.save_demand_score(RUN, cid, {
                "pain_intensity": 5, "frequency": 5, "willingness_to_pay": 5, "reachability": 5,
                "demand_score": demand, "persistence_score": persistence,
                "evidence_count": 6, "distinct_authors": 5})
            self.store.save_soft_filter(RUN, cid, solvable, 0.9, "", [])
        rank_run(self.store, RUN, allowed_solvability=["yes", "partial"])
        report_run(self.store, RUN, self.dir, max_ranked_themes=1)
        self.text = (Path(self.dir) / f"{RUN}.md").read_text(encoding="utf-8")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_counts_only_themes_that_actually_rank(self):
        self.assertIn("Showing top 1 of 2 ranked themes", self.text)
        self.assertIn("5 themes total; 3 were dropped at rank", self.text)

    def test_formula_matches_stage_9(self):
        """Rank multiplies the BOUNDED persistence multiplier, not the raw 1-10 score, and the
        report must print the factors rank used and a product equal to the stored score."""
        top = self.store.get_ranked_clusters(RUN)[0]
        demand, multiplier, weight = map(float, re.search(
            r"Rank formula: demand ([\d.]+) x persistence multiplier ([\d.]+) "
            r"x solvable weight ([\d.]+)", self.text).groups())
        score = float(re.search(r"Rank score: ([\d.]+)", self.text).group(1))
        self.assertEqual((demand, multiplier, weight), (
            top["demand_score"], round(top["rank_breakdown"]["persistence_multiplier"], 2),
            round(top["solvable_weight"], 2)))
        self.assertEqual(score, top["rank_score"])
        self.assertEqual(round(demand * multiplier * weight, 2), score)
        self.assertNotIn("(1 + saturation", self.text)


if __name__ == "__main__":
    unittest.main()
