"""Guard the stage-9 solvability gate against silent unwiring.

`rank_run(allowed_solvability=None)` means "no gate configured" and ranks everything.
That is correct as a default, but it makes a MISSING argument indistinguishable from a
deliberately disabled gate -- so a call site that simply forgets the parameter turns the
gate off with no error, no warning, and no visible change except unsolvable themes
appearing in the ranked output.

That is exactly what happened: pipeline/analyze.py and pipeline/resume.py passed
`allowed_solvability`, webapp/app.py did not, so every run started from the GUI ranked
without the gate. The synthetic benchmark caught it -- run dd952fff8d8f ranked a theme
carrying solvable=no and ["visa_immigration_process", "requires_in_person"] at #6 and
turned it into an idea.

These tests assert the wiring, not the ranking maths, because the failure was wiring.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every place that invokes stage 9. Adding a fourth caller means adding it here.
RANK_CALL_SITES = [
    ROOT / "webapp" / "app.py",
    ROOT / "pipeline" / "analyze.py",
    ROOT / "pipeline" / "resume.py",
]


def _rank_run_calls(path: Path):
    """Yield every ast.Call node that invokes rank_run in the given file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name == "rank_run":
            yield node


class RankGateWiringTest(unittest.TestCase):
    def test_every_call_site_passes_allowed_solvability(self):
        for path in RANK_CALL_SITES:
            calls = list(_rank_run_calls(path))
            self.assertTrue(calls, f"no rank_run call found in {path.name} -- "
                                   "did it move? update RANK_CALL_SITES")
            for call in calls:
                kwargs = {kw.arg for kw in call.keywords if kw.arg}
                self.assertIn(
                    "allowed_solvability", kwargs,
                    f"{path.name}:{call.lineno} calls rank_run without "
                    "allowed_solvability -- the solvability gate is off for this path",
                )

    def test_every_call_site_passes_min_support(self):
        """Same failure shape, same silent outcome: the support thresholds are the only
        other gate stage 9 owns."""
        for path in RANK_CALL_SITES:
            for call in _rank_run_calls(path):
                kwargs = {kw.arg for kw in call.keywords if kw.arg}
                self.assertIn(
                    "min_support", kwargs,
                    f"{path.name}:{call.lineno} calls rank_run without min_support",
                )

    def test_config_actually_defines_the_gate(self):
        """A wired call site reading an absent config key is the same bug one level up."""
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        allowed = cfg.get("rank", {}).get("allowed_solvability")
        self.assertTrue(allowed, "config.yaml rank.allowed_solvability is missing or empty; "
                                 "every call site would rank with the gate off")
        self.assertNotIn("no", [str(v).strip().lower() for v in allowed],
                         "'no' in allowed_solvability defeats the gate")


class RankGateBehaviourTest(unittest.TestCase):
    """The gate itself, independent of who calls it."""

    def _rank_one(self, solvable, allowed):
        from pipeline.s9_rank import rank_run

        # (cluster_id, demand, persistence, saturation, dropped, warnings, solvable)
        row = ("c1", 5.0, 3.0, 0.0, 0, "[]", solvable)

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def __iter__(self):
                return iter(self._rows)

        class _Conn:
            def execute(self, sql, params=None):
                # Only the cluster sweep needs rows; support/other lookups get nothing,
                # which is the same as "no min_support configured".
                return _Cursor([row] if "FROM clusters c" in sql else [])

        class _Store:
            conn = _Conn()

            def __getattr__(self, _name):
                # Swallow whatever persistence call rank_run makes -- this test asserts the
                # gate decision, not the write path.
                return lambda *a, **k: None

        return rank_run(_Store(), "run1", allowed_solvability=allowed)

    def test_unsolvable_theme_is_dropped_when_gate_is_on(self):
        out = self._rank_one("no", ["yes", "partial"])
        self.assertEqual(out.get("ranked"), 0, "a solvable=no theme was ranked")
        self.assertEqual(out.get("dropped"), 1)

    def test_unsolvable_theme_survives_when_gate_is_off(self):
        """Documents the default. This is why a missing argument is invisible."""
        out = self._rank_one("no", None)
        self.assertEqual(out.get("ranked"), 1)
        self.assertEqual(out.get("dropped"), 0)


if __name__ == "__main__":
    unittest.main()
