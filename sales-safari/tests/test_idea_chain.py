"""Tests for the idea chain: rank -> ideas -> competitors OF those ideas -> reviews -> brief.

The behaviours pinned here are the ones that made run 9af5b27db46e's output unusable:
competitors attached to themes nobody turned into an idea, magazines counted as software
competition, refusals shipped as idea titles, and saturation penalising the only themes we
actually understood.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import s9b_competitors, s10b_brief
from pipeline.s9_rank import rank_run
from pipeline.s10_ideas import ideas_run


class _ChainStore:
    """In-memory stand-in covering just the surface the idea-chain stages touch."""

    def __init__(self, clusters=None, ideas=None, competitors=None, reviews=None):
        self.clusters = clusters or []
        self.ideas = ideas or []
        self.competitors = competitors or []
        self.reviews = reviews or {}
        self.briefs = []
        self.saturation = {}
        self.stage = None

    def get_cluster_details(self, run_id):
        return self.clusters

    def get_ideas(self, run_id):
        return self.ideas

    def clear_competitors(self, run_id):
        self.competitors = []

    def save_competitor(self, run_id, cluster_id, c):
        cid = f"c{len(self.competitors)}"
        self.competitors.append({**c, "id": cid, "cluster_id": cluster_id})
        return cid

    def get_competitors(self, run_id, cluster_id=None):
        return [c for c in self.competitors
                if cluster_id is None or c["cluster_id"] == cluster_id]

    def get_reviews(self, run_id, competitor_id=None):
        return self.reviews.get(competitor_id, [])

    def competitor_counts(self, run_id):
        counts = {}
        for c in self.competitors:
            counts[c["cluster_id"]] = counts.get(c["cluster_id"], 0) + 1
        return counts

    def set_saturation(self, run_id, cluster_id, score, incumbent_count=0):
        self.saturation[cluster_id] = (score, incumbent_count)

    def clear_briefs(self, run_id):
        self.briefs = []

    def save_brief(self, run_id, idea_id, cluster_id, b):
        self.briefs.append({**b, "idea_id": idea_id, "cluster_id": cluster_id})

    def set_stage(self, run_id, stage, status):
        self.stage = (stage, status)


def _cluster(cid, label="theme", pains=None):
    return {"id": cid, "label": label,
            "pains": pains or [{"complaint": "it breaks", "persona_canonical": "nomads",
                                "source_permalink": "https://example.test/p"}]}


class CompetitorDiscoveryTest(unittest.TestCase):
    def _run(self, model_reply, **kwargs):
        store = _ChainStore(
            clusters=[_cluster("t1")],
            ideas=[{"id": "i1", "cluster_id": "t1", "title": "Vault", "pitch": "locks apps"}])
        with mock.patch.object(s9b_competitors, "_call_extractor",
                               return_value=(json.dumps(model_reply), "claude")):
            result = s9b_competitors.competitors_run(
                store, "run1", verify_urls=False, **kwargs)
        return store, result

    def test_competitors_attach_to_the_idea_cluster(self):
        """The whole point of the reorder: what 9b finds must hang off an idea, because
        that is the only place the UI can show it and the brief can use it."""
        store, result = self._run([{"id": "t1", "competitors": [
            {"name": "1Password", "url": "https://1password.com", "category": "password manager"}]}])
        self.assertEqual(result["competitors"], 1)
        self.assertEqual(store.get_competitors("run1", "t1")[0]["name"], "1Password")

    def test_non_software_competitors_are_rejected(self):
        """Run 9af5b27db46e stored The Atlantic and VICE as competitors, then mined their
        App Store reviews as 'incumbent gaps'."""
        store, result = self._run([{"id": "t1", "competitors": [
            {"name": "The Atlantic", "url": "https://theatlantic.com", "category": "journalism"},
            {"name": "Stop AAPI Hate", "url": "https://stopaapihate.org", "category": "advocacy"},
            {"name": "Bumble", "url": "https://bumble.com", "category": "dating app"},
        ]}])
        self.assertEqual(result["rejected"], 2)
        self.assertEqual([c["name"] for c in store.get_competitors("run1", "t1")], ["Bumble"])

    def test_dead_urls_are_dropped_by_the_grounding_gate(self):
        store = _ChainStore(
            clusters=[_cluster("t1")],
            ideas=[{"id": "i1", "cluster_id": "t1", "title": "Vault", "pitch": "locks apps"}])
        reply = [{"id": "t1", "competitors": [
            {"name": "RealCo", "url": "https://real.test", "category": "saas"},
            {"name": "FakeCo", "url": "https://fake.test", "category": "saas"},
        ]}]
        with mock.patch.object(s9b_competitors, "_call_extractor",
                               return_value=(json.dumps(reply), "claude")), \
             mock.patch.object(s9b_competitors, "url_is_live",
                               side_effect=lambda url, dom="", t=8: "real" in url):
            result = s9b_competitors.competitors_run(store, "run1", verify_urls=True)
        self.assertEqual(result["unverified"], 1)
        self.assertEqual([c["name"] for c in store.get_competitors("run1")], ["RealCo"])

    def test_no_ideas_means_no_work(self):
        store = _ChainStore(clusters=[_cluster("t1")], ideas=[])
        result = s9b_competitors.competitors_run(store, "run1", verify_urls=False)
        self.assertEqual(result, {"ideas": 0, "competitors": 0, "covered": 0,
                                  "rejected": 0, "unverified": 0})

    def test_batch_failure_is_survivable(self):
        store = _ChainStore(
            clusters=[_cluster("t1")],
            ideas=[{"id": "i1", "cluster_id": "t1", "title": "Vault", "pitch": "x"}])
        with mock.patch.object(s9b_competitors, "_call_extractor",
                               side_effect=RuntimeError("API 500")):
            result = s9b_competitors.competitors_run(store, "run1", verify_urls=False)
        self.assertEqual(result["competitors"], 0)
        self.assertEqual(store.saturation["t1"], (0.0, 0))


class RankFormulaTest(unittest.TestCase):
    """Saturation must not move rank. It used to divide, so the 8 themes we had found
    competitors for were pushed to ranks 151 and 209-213 of 213."""

    class _RankStore:
        def __init__(self, rows):
            self.rows = rows
            self.saved = []

            class _Conn:
                def __init__(self, rows):
                    self.rows = rows

                def execute(self, *a, **k):
                    return self

                def fetchall(self):
                    return self.rows
            self.conn = _Conn(rows)

        def get_cluster_details(self, run_id):
            return []

        def clear_rankings(self, run_id):
            self.saved = []

        def save_ranking(self, run_id, row):
            self.saved.append(row)

        def set_stage(self, run_id, stage, status):
            pass

    def test_saturated_theme_is_not_penalised(self):
        # (id, demand, persistence, saturation, dropped, warnings, solvable)
        store = self._RankStore([
            ("saturated", 6.0, 3.0, 8.0, 0, "[]", "yes"),
            ("unknown", 6.0, 3.0, 0.0, 0, "[]", "yes"),
        ])
        rank_run(store, "run1")
        scores = {r["cluster_id"]: r["rank_score"] for r in store.saved}
        self.assertEqual(scores["saturated"], scores["unknown"])
        self.assertEqual(scores["saturated"], 18.0)


class IdeaDraftTest(unittest.TestCase):
    class _IdeaStore(_ChainStore):
        def __init__(self, clusters, ranked):
            super().__init__(clusters=clusters)
            self.ranked = ranked
            self.saved = []

        def get_ranked_clusters(self, run_id):
            return self.ranked

        def clear_ideas(self, run_id):
            self.saved = []

        def save_idea(self, run_id, cluster_id, title, pitch, permalink):
            self.saved.append({"cluster_id": cluster_id, "title": title, "pitch": pitch})

    def test_skipped_themes_are_backfilled_not_shipped_as_refusals(self):
        """Run 9af5b27db46e shipped 3 of 5 ideas titled 'Software-adjacent only: ...'."""
        clusters = [_cluster(f"t{i}") for i in range(1, 5)]
        ranked = [{"cluster_id": f"t{i}"} for i in range(1, 5)]
        store = self._IdeaStore(clusters, ranked)
        reply = [
            {"id": "t1", "skip": True, "reason": "interpersonal, not software"},
            {"id": "t2", "title": "Real Idea A", "pitch": "does a thing"},
            {"id": "t3", "skip": True, "reason": "meta-complaint"},
            {"id": "t4", "title": "Real Idea B", "pitch": "does another thing"},
        ]
        with mock.patch("pipeline.s10_ideas._call_extractor",
                        return_value=(json.dumps(reply), "claude")):
            result = ideas_run(store, "run1", top_n=2, overshoot=2)
        self.assertEqual(result["ideas"], 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual([s["title"] for s in store.saved], ["Real Idea A", "Real Idea B"])

    def test_template_fallback_still_works_when_llm_is_down(self):
        clusters = [_cluster("t1")]
        store = self._IdeaStore(clusters, [{"cluster_id": "t1"}])
        with mock.patch("pipeline.s10_ideas._call_extractor",
                        side_effect=RuntimeError("connection refused")):
            result = ideas_run(store, "run1", top_n=1)
        self.assertEqual(result["ideas"], 1)
        self.assertEqual(result["from_template"], 1)


class BriefTest(unittest.TestCase):
    def _store(self, reviews=None):
        store = _ChainStore(
            clusters=[_cluster("t1", pains=[{"complaint": "fees eat me",
                                             "persona_canonical": "nomads"}])],
            ideas=[{"id": "i1", "cluster_id": "t1", "title": "Vault", "pitch": "locks apps"}])
        store.competitors = [{"id": "c0", "cluster_id": "t1", "name": "Wise",
                              "url": "https://wise.com", "note": "transfers",
                              "weakness": "slow support"}]
        store.reviews = reviews or {}
        return store

    def test_wedge_with_real_quote_is_marked_evidenced(self):
        store = self._store({"c0": [{"body": "Support took three weeks to reply",
                                     "rating": 1, "source_url": "https://apps.test/wise"}]})
        reply = [{"id": "t1", "problem": "p", "target_user": "u", "wedge": "w",
                  "incumbents": [{"name": "Wise", "fails_at": "support",
                                  "quote": "Support took three weeks to reply"}],
                  "mvp": ["a", "b"], "risks": ["r"]}]
        with mock.patch.object(s10b_brief, "_call_extractor",
                               return_value=(json.dumps(reply), "claude")):
            result = s10b_brief.brief_run(store, "run1")
        self.assertEqual(result["with_review_evidence"], 1)
        brief = store.briefs[0]
        self.assertTrue(brief["has_review_evidence"])
        self.assertEqual(brief["incumbents"][0]["source_url"], "https://apps.test/wise")

    def test_invented_quote_is_dropped_and_not_counted_as_evidence(self):
        """The model must not be able to manufacture the gap the wedge rests on."""
        store = self._store({"c0": [{"body": "Real complaint about fees", "rating": 2,
                                     "source_url": "https://apps.test/wise"}]})
        reply = [{"id": "t1", "problem": "p", "target_user": "u", "wedge": "w",
                  "incumbents": [{"name": "Wise", "fails_at": "support",
                                  "quote": "This quote was never in the input"}],
                  "mvp": [], "risks": []}]
        with mock.patch.object(s10b_brief, "_call_extractor",
                               return_value=(json.dumps(reply), "claude")):
            s10b_brief.brief_run(store, "run1")
        brief = store.briefs[0]
        self.assertEqual(brief["incumbents"][0]["quote"], "")
        self.assertFalse(brief["has_review_evidence"])
        self.assertEqual(brief["review_quote_count"], 0)

    def test_no_reviews_yields_an_honest_unproven_brief(self):
        """Web-only SaaS has no App Store presence; the brief must admit that rather than
        inventing a gap to fill the field."""
        store = self._store({})
        reply = [{"id": "t1", "problem": "p", "target_user": "u",
                  "wedge": "gap unproven; would need to check support SLAs",
                  "incumbents": [{"name": "Wise", "fails_at": "unknown", "quote": ""}],
                  "mvp": ["a"], "risks": ["r"]}]
        with mock.patch.object(s10b_brief, "_call_extractor",
                               return_value=(json.dumps(reply), "claude")):
            result = s10b_brief.brief_run(store, "run1")
        self.assertEqual(result["with_review_evidence"], 0)
        self.assertFalse(store.briefs[0]["has_review_evidence"])

    def test_llm_failure_falls_back_without_claiming_evidence(self):
        store = self._store({})
        with mock.patch.object(s10b_brief, "_call_extractor",
                               side_effect=RuntimeError("API 429")):
            result = s10b_brief.brief_run(store, "run1")
        self.assertEqual(result["briefs"], 1)
        self.assertEqual(result["from_llm"], 0)
        self.assertFalse(store.briefs[0]["has_review_evidence"])


if __name__ == "__main__":
    unittest.main()
