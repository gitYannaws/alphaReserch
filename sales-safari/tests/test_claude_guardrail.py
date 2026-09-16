"""Tests for the Claude CLI hourly budget guardrail and pacing."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pipeline import extract


class ClaudeGuardrailTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "claude_usage.json"
        self.guard_cfg = {
            "enabled": True,
            "max_calls_per_hour": 3,
            "budget_window_seconds": 3600.0,
            "min_call_interval_seconds": 0.05,
            "state_file": str(self.state_file),
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_guardrail_allows_calls_within_budget(self):
        extract._check_claude_guardrail(self.guard_cfg)
        time.sleep(0.06)
        extract._check_claude_guardrail(self.guard_cfg)
        
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)

    def test_guardrail_blocks_calls_over_budget(self):
        for _ in range(3):
            extract._check_claude_guardrail(self.guard_cfg)
            time.sleep(0.06)

        with self.assertRaises(RuntimeError) as ctx:
            extract._check_claude_guardrail(self.guard_cfg)
        self.assertIn("hourly budget exceeded", str(ctx.exception))
        self.assertIn("3/3", str(ctx.exception))

    def test_guardrail_prunes_expired_calls(self):
        old_time = time.time() - 7200
        self.state_file.write_text(json.dumps([old_time, old_time + 10]), encoding="utf-8")
        
        extract._check_claude_guardrail(self.guard_cfg)
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)

    def test_fallback_on_budget_exceeded(self):
        extract_cfg = {
            "providers": ["claude", "codex"],
            "claude_guardrail": {
                "enabled": True,
                "max_calls_per_hour": 1,
                "state_file": str(self.state_file),
            },
            "codex": {"model": "mock-model"},
        }
        extract._check_claude_guardrail(extract_cfg["claude_guardrail"])

        with mock.patch.object(extract, "_call_codex", return_value="fallback-success"):
            raw, provider = extract._call_extractor("prompt", extract_cfg)
            self.assertEqual(raw, "fallback-success")
            self.assertEqual(provider, "codex")


if __name__ == "__main__":
    unittest.main()
