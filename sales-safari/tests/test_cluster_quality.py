"""Stage 5 must leave an auditable record of what it kept, what it threw away, and how good
each theme is.

On run pbfamily0001, 39,182 of 72,344 eligible pains (54.2%) were HDBSCAN noise and got no
row anywhere; the count lived only in a return dict and the stage log said `clusters=`. The
`min_samples` and `cluster_selection_epsilon` knobs the HDBSCAN docs name for exactly this
were never set. No theme-quality number was persisted, and a theme with no audit label was
named after whichever member SQLite returned first.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import numpy as np

from pipeline.cluster import cluster_run
from pipeline.store import Store

RUN = "run1"
DIM = 8


def _axis(k, scale=10.0):
    v = np.zeros(DIM, dtype="float32")
    v[k] = scale
    return v


class ClusterRecordTests(unittest.TestCase):
    """Three tight blobs of four pains on separate axes, plus two far outliers. Fewer than
    15 points, so HDBSCAN sees the raw vectors and the expected labeling is exact."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))
        rng = np.random.default_rng(0)
        self.vecs = {}
        self.blobs = []
        for k in range(3):
            # The exact centre is inserted LAST so neither insertion nor id order picks it.
            offsets = [rng.normal(0, 0.05, DIM) for _ in range(3)] + [np.zeros(DIM)]
            blob = []
            for j, off in enumerate(offsets):
                text = (f"blob {k} central complaint" if j == 3
                        else f"blob {k} peripheral complaint {j}")
                blob.append(self._pain(f"b{k}-{j}", text, _axis(k) + off))
            self.blobs.append(blob)
        self.outliers = [self._pain(f"o{j}", f"outlier complaint number {j}", _axis(j, 30.0))
                         for j in (5, 6)]

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pain(self, doc, text, vec):
        self.store.insert_pain(RUN, {"document_id": doc, "verbatim_span": text,
                                     "complaint": text, "author_hash": f"author-{doc}"})
        pid = self.store.conn.execute(
            "SELECT id FROM pains WHERE document_id=?", (doc,)).fetchone()[0]
        self.store.save_embedding(RUN, pid, np.asarray(vec, dtype="float32").tobytes())
        self.vecs[pid] = np.asarray(vec, dtype="float64")
        return pid

    def _noise_flags(self):
        return dict(self.store.conn.execute(
            "SELECT id, cluster_noise FROM pains WHERE run_id=?", (RUN,)).fetchall())

    def test_noise_is_persisted_per_pain_and_cleared_with_the_clusters(self):
        stats = cluster_run(self.store, RUN, min_cluster_size=2, min_cohesion=0.80)
        self.assertEqual(stats["clusters"], 3)
        self.assertEqual(stats["noise"], 2)
        flags = self._noise_flags()
        for pid in self.outliers:
            self.assertEqual(flags[pid], 1, "an outlier must be flagged as noise")
        for pid in (p for blob in self.blobs for p in blob):
            self.assertEqual(flags[pid], 0, "a theme member must be flagged as kept")

        self.store.clear_clusters(RUN)
        self.assertEqual(set(self._noise_flags().values()), {None})

    def test_min_samples_reaches_hdbscan(self):
        """No blob has five members, so min_samples=5 leaves nothing dense enough to keep."""
        stats = cluster_run(self.store, RUN, min_cluster_size=2, min_samples=5)
        self.assertEqual(stats["clusters"], 0)
        self.assertEqual(set(self._noise_flags().values()), {1},
                         "every pain left outside a theme is recorded as noise")

    def test_selection_epsilon_reaches_hdbscan(self):
        """Blobs sit ~14 apart; an epsilon of 20 stops HDBSCAN splitting two of them.
        The cohesion guard is off so it cannot re-split what epsilon merged."""
        stats = cluster_run(self.store, RUN, min_cluster_size=2, min_cohesion=0.0,
                            cluster_selection_epsilon=20.0)
        self.assertEqual(stats["clusters"], 2)

    def test_dbcv_is_reported(self):
        stats = cluster_run(self.store, RUN, min_cluster_size=2)
        self.assertIsInstance(stats["dbcv"], float)
        self.assertGreater(stats["dbcv"], 0.5, "three clean blobs are a well-separated labeling")

    def test_theme_quality_is_stored(self):
        cluster_run(self.store, RUN, min_cluster_size=2, min_cohesion=0.80)
        themes = self.store.get_cluster_details(RUN)
        self.assertEqual(len(themes), 3)
        for theme in themes:
            members = np.array([self.vecs[p["id"]] for p in theme["pains"]])
            unit = members / np.linalg.norm(members, axis=1, keepdims=True)
            centroid = unit.mean(axis=0)
            expected = float((unit @ (centroid / np.linalg.norm(centroid))).mean())
            self.assertGreaterEqual(theme["cohesion"], 0.0)
            self.assertLessEqual(theme["cohesion"], 1.0)
            self.assertAlmostEqual(theme["cohesion"], expected, places=4)
            self.assertEqual(theme["audit_label_source"], "quote",
                             "no semantic audit ran, so the name is a member quote")

    def test_a_quote_label_comes_from_the_most_central_member(self):
        cluster_run(self.store, RUN, min_cluster_size=2, min_cohesion=0.80)
        labels = sorted(t["label"] for t in self.store.get_cluster_details(RUN))
        self.assertEqual(labels, [f"blob {k} central complaint" for k in range(3)])


if __name__ == "__main__":
    unittest.main()
