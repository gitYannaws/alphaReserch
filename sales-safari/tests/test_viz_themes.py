"""The opportunity quadrant reads WTP, intensity and the surviving rank per theme.

The chart splits themes into painkiller / vitamin corners on `wtp` x `intensity`, and
labels only themes that survived stage 9. A dropped theme must come back with rank None,
otherwise the chart would put a name on something the pipeline already discarded.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.store import Store                      # noqa: E402
from tests.test_pain_paging import _seed              # noqa: E402


class VizThemesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))
        _seed(self.store)
        c = self.store.conn
        # A second theme, dropped at rank; c1 from the seed survives at rank 1.
        c.execute("INSERT INTO clusters (id,run_id,label,size,distinct_authors) "
                  "VALUES (?,?,?,?,?)", ("c2", "r1", "Bank deposits", 1, 1))
        c.execute("INSERT INTO demand_scores (cluster_id,run_id,pain_intensity,frequency,"
                  "willingness_to_pay,reachability,demand_score,evidence_count,distinct_authors) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", ("c2", "r1", 3.0, 1, 1.0, 4, 2.25, 1, 1))
        c.execute("INSERT INTO rankings (cluster_id,run_id,rank,rank_score,demand_score,"
                  "persistence_score,saturation_score,dropped,filter_reasons) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", ("c1", "r1", 1, 40.0, 6.25, 6.4, 0, 0, "[]"))
        c.execute("INSERT INTO rankings (cluster_id,run_id,rank,rank_score,demand_score,"
                  "persistence_score,saturation_score,dropped,filter_reasons) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", ("c2", "r1", 2, 5.0, 2.25, 1.0, 0, 1, '["min_support"]'))
        c.commit()

    def tearDown(self):
        self.store.close()

    def test_axes_and_rank(self):
        out = self.store.get_viz_themes("r1")
        by_id = {t["id"]: t for t in out["themes"]}
        self.assertEqual(out["total"], 2)
        self.assertEqual(by_id["c1"]["wtp"], 3)
        self.assertEqual(by_id["c1"]["intensity"], 7.5)
        self.assertEqual(by_id["c1"]["rank"], 1)
        self.assertEqual(by_id["c1"]["solvable"], "yes")

    def test_dropped_theme_has_no_rank(self):
        out = self.store.get_viz_themes("r1")
        by_id = {t["id"]: t for t in out["themes"]}
        self.assertIsNone(by_id["c2"]["rank"])
        self.assertEqual(by_id["c2"]["wtp"], 1.0)

    def test_ranked_theme_survives_the_author_cap(self):
        # c1 (2 authors, rank 1) is below c2? No: give c2 more authors so the cap of one
        # theme would keep only c2, then check the ranked c1 still comes back.
        c = self.store.conn
        c.execute("UPDATE demand_scores SET distinct_authors=9, demand_score=9 WHERE cluster_id='c2'")
        c.commit()
        out = self.store.get_viz_themes("r1", limit=1)
        ids = {t["id"] for t in out["themes"]}
        self.assertEqual(ids, {"c1", "c2"})
        self.assertEqual(out["total"], 2)

    def test_dropped_theme_outside_the_cap_is_left_out(self):
        c = self.store.conn
        c.execute("UPDATE demand_scores SET distinct_authors=9, demand_score=9 WHERE cluster_id='c1'")
        c.commit()
        out = self.store.get_viz_themes("r1", limit=1)
        self.assertEqual({t["id"] for t in out["themes"]}, {"c1"})

    def test_theme_without_ranking_row(self):
        self.store.conn.execute("DELETE FROM rankings WHERE cluster_id='c1'")
        self.store.conn.commit()
        out = self.store.get_viz_themes("r1")
        by_id = {t["id"]: t for t in out["themes"]}
        self.assertIsNone(by_id["c1"]["rank"])


if __name__ == "__main__":
    unittest.main()
