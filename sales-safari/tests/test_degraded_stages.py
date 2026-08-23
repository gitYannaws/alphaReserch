"""A stage that falls back to its stub must SAY SO.

Stages 10a (ideas), 9b (competitors) and 10b (brief) each catch their own LLM failure and
continue with a keyword template. That is right -- one bad call must not sink a 40-hour run.
It was also silent. Each stage reported the reason with print(), and the detached resume
runner captures only its own logger, so on run pbfamily0001 the single clue that the entire
idea chain had produced nothing real was the substring "template=25" inside an OK line. The
run reported status=done and the report read as finished work.

These tests assert the reporting, not the LLM behaviour, because the failure was reporting.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from pipeline import s10_ideas, s10b_brief, s9b_competitors


class _Store:
    def __init__(self):
        self.saved = []

    # -- shared -----------------------------------------------------------
    def get_cluster_details(self, run_id, cluster_ids=None):
        return [{"id": "c1", "label": "wire fees", "size": 6, "distinct_authors": 5,
                 "pains": [{"complaint": "wire fees hurt", "verbatim_span": "wire fees hurt",
                            "source_permalink": "https://example.test/1"}]}]

    def get_competitors(self, run_id, cluster_id=None):
        return []

    def get_reviews(self, run_id, competitor_id=None):
        return []

    def set_stage(self, run_id, stage, status):
        pass

    # -- ideas ------------------------------------------------------------
    def get_ranked_clusters(self, run_id, include_dropped=False):
        return [{"cluster_id": "c1", "rank": 1}]

    def clear_ideas(self, run_id):
        pass

    def save_idea(self, run_id, cluster_id, title, pitch, permalink):
        self.saved.append(title)

    # -- competitors ------------------------------------------------------
    def get_ideas(self, run_id):
        return [{"id": "i1", "cluster_id": "c1", "title": "T", "pitch": "P"}]

    def clear_competitors(self, run_id):
        pass

    def competitor_counts(self, run_id):
        return {}

    def set_saturation(self, run_id, cluster_id, score, incumbent_count=0):
        pass

    # -- brief ------------------------------------------------------------
    def clear_briefs(self, run_id):
        pass

    def save_brief(self, run_id, idea_id, cluster_id, brief):
        self.saved.append(brief)


def _boom(prompt, cfg):
    raise RuntimeError("claude exit 1: API 429: usage limit reached")


class DegradedStagesReportWhy(unittest.TestCase):
    def test_ideas_reports_the_llm_error(self):
        original = s10_ideas._call_extractor
        s10_ideas._call_extractor = _boom
        try:
            r = s10_ideas.ideas_run(_Store(), "run", top_n=1, extract_cfg={}, model="m")
        finally:
            s10_ideas._call_extractor = original
        self.assertEqual(r["from_llm"], 0)
        self.assertEqual(r["from_template"], 1, "the stub must still produce an idea")
        self.assertIn("usage limit", r["llm_error"])

    def test_ideas_reports_an_empty_answer_too(self):
        """An LLM that answers with nothing is a failure the old code could not see."""
        original = s10_ideas._call_extractor
        s10_ideas._call_extractor = lambda prompt, cfg: ("no ideas here", "test")
        try:
            r = s10_ideas.ideas_run(_Store(), "run", top_n=1, extract_cfg={}, model="m")
        finally:
            s10_ideas._call_extractor = original
        self.assertEqual(r["from_llm"], 0)
        self.assertIn("no usable ideas", r["llm_error"])

    def test_ideas_success_reports_no_error(self):
        raw = '[{"id": "c1", "title": "WireWatch: fee tracking", "pitch": "It tracks fees."}]'
        original = s10_ideas._call_extractor
        s10_ideas._call_extractor = lambda prompt, cfg: (raw, "test")
        try:
            r = s10_ideas.ideas_run(_Store(), "run", top_n=1, extract_cfg={}, model="m")
        finally:
            s10_ideas._call_extractor = original
        self.assertEqual(r["from_llm"], 1)
        self.assertEqual(r["llm_error"], "")

    def test_competitors_counts_failed_batches(self):
        original = s9b_competitors._call_extractor
        s9b_competitors._call_extractor = _boom
        try:
            r = s9b_competitors.competitors_run(_Store(), "run", extract_cfg={}, model="m",
                                                verify_urls=False)
        finally:
            s9b_competitors._call_extractor = original
        self.assertEqual(r["competitors"], 0)
        self.assertEqual(r["batch_errors"], 1)
        self.assertIn("usage limit", r["llm_error"])

    def test_brief_reports_the_llm_error(self):
        original = s10b_brief._call_extractor
        s10b_brief._call_extractor = _boom
        try:
            r = s10b_brief.brief_run(_Store(), "run", extract_cfg={}, model="m")
        finally:
            s10b_brief._call_extractor = original
        self.assertEqual(r["from_llm"], 0)
        self.assertEqual(r["briefs"], 1, "the thin fallback must still produce a brief")
        self.assertIn("usage limit", r["llm_error"])


class RunnerSurfacesDegradation(unittest.TestCase):
    """The stages now report `llm_error`. Assert the runner does not drop it on the floor."""

    RESUME = Path(__file__).resolve().parents[1] / "pipeline" / "resume.py"

    def _method(self, name):
        tree = ast.parse(self.RESUME.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.dump(node)
        self.fail(f"Runner.{name} not found in resume.py")

    def test_each_fallback_stage_checks_llm_error(self):
        for name in ("_ideas", "_competitors", "_brief"):
            with self.subTest(stage=name):
                body = self._method(name)
                self.assertIn("llm_error", body,
                              f"Runner.{name} must read llm_error from the stage result")
                self.assertIn("_warn_degraded", body,
                              f"Runner.{name} must warn when the stage degraded")

    def test_a_degraded_run_says_so_at_the_end(self):
        text = self.RESUME.read_text(encoding="utf-8")
        self.assertIn("INCOMPLETE", text,
                      "the final NOTIFY must name a run that finished on fallback paths")


if __name__ == "__main__":
    unittest.main()
