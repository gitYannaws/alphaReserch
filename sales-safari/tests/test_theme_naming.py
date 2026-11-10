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

import unittest
from pathlib import Path

from pipeline import cluster as cluster_mod
from pipeline.s12_report import report_run

ROOT = Path(__file__).resolve().parents[1]


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


class _ReportStore:
    """Two ranked themes and three dropped ones."""

    def get_run(self, run_id):
        return {"job_id": run_id, "seed_url": "https://example.test"}

    def get_ranked_clusters(self, run_id, include_dropped=False):
        rows = []
        for i in range(5):
            dropped = i >= 2
            rows.append({
                "rank": None if dropped else i + 1,
                # 4.0 demand x 0.86 persistence multiplier (raw 3.0) x 1.0 = 3.44
                "rank_score": 0.0 if dropped else 3.44,
                "demand_score": 4.0,
                "persistence_score": 3.0,
                "saturation_score": 2.0,
                "solvable_weight": 1.0,
                "dropped": dropped,
                "filter_reasons": [],
                "cluster_id": f"c{i}",
                "label": f"theme {i}",
                "size": 6,
                "distinct_authors": 5,
                "gap_summary": "",
                "solvable": "yes",
                "confidence": 0.9,
                "reason": "",
                "pain_intensity": 5.0,
                "frequency": 10.0,
                "willingness_to_pay": 1.0,
                "reachability": 2.0,
                "recurrence_score": 10.0,
                "scoring_evidence": {},
                "rank_breakdown": {"persistence": 3.0, "persistence_multiplier": 0.86},
            })
        return rows if include_dropped else [r for r in rows if not r["dropped"]]

    def get_ideas(self, run_id):
        return []

    def get_cluster_details(self, run_id):
        return []

    def get_competitors(self, run_id, cluster_id=None):
        return []

    def get_reviews(self, run_id, competitor_id=None):
        return []

    def save_report(self, run_id, path):
        pass

    def set_stage(self, run_id, stage, status):
        pass


class ReportHonestyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        report_run(_ReportStore(), "run", self.dir, max_ranked_themes=1)
        self.text = (Path(self.dir) / "run.md").read_text(encoding="utf-8")

    def test_counts_only_themes_that_actually_rank(self):
        self.assertIn("Showing top 1 of 2 ranked themes", self.text)
        self.assertIn("5 themes total; 3 were dropped at rank", self.text)

    def test_formula_matches_stage_9(self):
        # Rank multiplies the BOUNDED persistence multiplier, not the raw 1-10 score; the
        # printed arithmetic must reproduce the stored rank score (4.00 x 0.86 x 1.00 = 3.44).
        self.assertIn("demand 4.00 x persistence multiplier 0.86 x solvable weight 1.00", self.text)
        self.assertIn("Rank score: 3.44", self.text)
        self.assertNotIn("x persistence 3.00 x", self.text)
        self.assertNotIn("(1 + saturation", self.text)


if __name__ == "__main__":
    unittest.main()
