import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import extract
from pipeline import cluster
from pipeline.s10_ideas import _idea_for
from pipeline.s7_filters import evaluate_cluster
from pipeline.s7b_softfilter import softfilter_run
from pipeline.s9_rank import _support_reasons


class _ExtractStore:
    def __init__(self):
        self.saved = []

    def get_documents(self, run_id):
        return [
            {
                "id": "advice",
                "source_url": "https://example.test/advice",
                "permalink": "https://example.test/advice",
                "title": "Phone safety",
                "raw_markdown": "This tracks. Better yet, have 2 phones.",
                "author_hash": "a1",
                "source_granularity": "post",
            },
            {
                "id": "pain",
                "source_url": "https://example.test/pain",
                "permalink": "https://example.test/pain",
                "title": "Bank transfers",
                "raw_markdown": "I hate paying wire fees every time I move money.",
                "author_hash": "a2",
                "source_granularity": "post",
            },
        ]

    def insert_pain(self, run_id, pain):
        self.saved.append(pain)
        return True


class _SoftFilterStore:
    def __init__(self, clusters):
        self.clusters = clusters
        self.saved = []
        self.stage = None

    def get_cluster_details(self, run_id):
        return self.clusters

    def clear_soft_filters(self, run_id):
        self.saved.clear()

    def save_soft_filter(self, run_id, cluster_id, solvable, confidence, reason):
        self.saved.append((cluster_id, solvable, confidence, reason))

    def set_stage(self, run_id, stage, status):
        self.stage = (stage, status)


class QualityGateTests(unittest.TestCase):
    def test_phi_filter_does_not_match_philippines(self):
        cluster = {
            "pains": [{
                "complaint": "Dating in the Phillipines is confusing.",
                "workflow_pain": "",
                "workaround": "",
                "wish": "",
                "verbatim_span": "Dating in the Phillipines is confusing.",
            }]
        }

        self.assertEqual(evaluate_cluster(cluster, ["requires_soc2_hipaa"]), [])

    def test_phi_filter_matches_actual_phi_term(self):
        cluster = {
            "pains": [{
                "complaint": "We need to avoid exposing PHI in support tickets.",
                "workflow_pain": "",
                "workaround": "",
                "wish": "",
                "verbatim_span": "avoid exposing PHI",
            }]
        }

        self.assertEqual(evaluate_cluster(cluster, ["requires_soc2_hipaa"]), ["requires_soc2_hipaa"])

    def test_extract_drops_workaround_only_advice(self):
        store = _ExtractStore()
        raw = json.dumps([
            {
                "post_id": "advice",
                "complaint": "",
                "workflow_pain": "",
                "workaround": "Carry two phones.",
                "wish": "",
                "persona": "traveler",
                "verbatim_span": "Better yet, have 2 phones.",
            },
            {
                "post_id": "pain",
                "complaint": "Wire fees are repeated and costly.",
                "workflow_pain": "",
                "workaround": "",
                "wish": "",
                "persona": "traveler",
                "verbatim_span": "I hate paying wire fees every time I move money.",
            },
        ])
        original = extract._call_extractor
        extract._call_extractor = lambda prompt, cfg: (raw, "test")
        try:
            stats = extract.extract_run(store, "run", batch_size=2)
        finally:
            extract._call_extractor = original

        self.assertEqual(stats["kept"], 1)
        self.assertEqual(stats["dropped"], 1)
        self.assertEqual(store.saved[0]["document_id"], "pain")

    def test_softfilter_batches_large_theme_sets(self):
        clusters = [
            {
                "id": f"c{i}",
                "label": f"theme {i}",
                "pains": [{"complaint": "manual work", "wish": "", "workflow_pain": ""}],
            }
            for i in range(5)
        ]
        store = _SoftFilterStore(clusters)
        calls = []

        def fake_call(prompt, cfg):
            payload = json.loads(prompt.split("THEMES:\n", 1)[1])
            calls.append([p["id"] for p in payload])
            return json.dumps([
                {"id": p["id"], "solvable": "yes", "confidence": 0.9, "reason": "software workflow"}
                for p in payload
            ]), "test"

        from pipeline import s7b_softfilter
        original = s7b_softfilter._call_extractor
        s7b_softfilter._call_extractor = fake_call
        try:
            stats = softfilter_run(store, "run", batch_size=2)
        finally:
            s7b_softfilter._call_extractor = original

        self.assertEqual([len(c) for c in calls], [2, 2, 1])
        self.assertEqual(stats["classified"], 5)
        self.assertEqual(len(store.saved), 5)

    def test_support_reasons_name_each_failed_threshold(self):
        reasons = _support_reasons(
            {"evidence_count": 4, "distinct_authors": 2, "distinct_threads": 1},
            {"evidence_count": 5, "distinct_authors": 4, "distinct_threads": 3},
        )

        self.assertEqual(
            reasons,
            ["insufficient_evidence", "insufficient_authors", "insufficient_threads"],
        )

    def test_semantic_cluster_refinement_drops_broad_context_matches(self):
        class Store:
            class Conn:
                def execute(self, query, params):
                    return self

                def fetchone(self):
                    return ("pain text", "", "", "")

            conn = Conn()

        original = cluster._call_extractor
        cluster._call_extractor = lambda prompt, cfg: (json.dumps([
            {"label": "Language translation failures", "pain_ids": ["language-1", "language-2"]},
            {"label": "Unrelated singleton", "pain_ids": ["scam-1"]},
        ]), "test")
        try:
            groups = cluster._semantic_groups(
                Store(), ["language-1", "language-2", "scam-1"], {}, min_cluster_size=2
            )
        finally:
            cluster._call_extractor = original

        self.assertEqual(groups, [(["language-1", "language-2"], "Language translation failures")])

    def test_idea_generation_city_fallback_has_consistent_shape(self):
        title, pitch, permalink = _idea_for({
            "label": "Medellin nightlife planning is hard",
            "pains": [{
                "complaint": "I cannot compare Medellin neighborhoods for nightlife.",
                "workflow_pain": "",
                "workaround": "",
                "wish": "",
                "persona": "solo traveler",
                "verbatim_span": "Medellin neighborhoods are hard to compare.",
                "source_permalink": "https://example.test/pain",
            }],
        })

        self.assertTrue(title.startswith("TripFit Brief:"))
        self.assertIn("solo traveler", pitch)
        self.assertEqual(permalink, "https://example.test/pain")


if __name__ == "__main__":
    unittest.main()
