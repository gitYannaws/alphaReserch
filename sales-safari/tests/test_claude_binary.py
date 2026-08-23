"""The Claude CLI must resolve without the interactive PATH, or say why it cannot.

This is the root cause of the whole pbfamily0001 idea-chain failure, and it took three
sessions to find because the evidence was thrown away twice over.

A Windows Scheduled Task does not inherit the interactive PATH. `claude` lives in
~/.local/bin, which is on PATH in a terminal and absent under schtasks, so every
Claude-backed stage died instantly with WinError 2. Stages 10a/9b/10b each caught that,
fell back to their keyword template, and reported the reason with print() -- which the
detached resume runner discards. The run finished status=done with 25 template "ideas",
0 competitors and 25 unproven briefs, and the only trace in the log was "template=25".

Confirmed directly in db/resume-pbfamily0001.log on 2026-08-23, when the guard restarted
the run through the Scheduled Task:

    claude: [WinError 2] The system cannot find the file specified

Two defences, both tested here: resolve the binary ourselves, and when we genuinely cannot,
raise an error that names the cause instead of letting subprocess raise WinError 2 forty
minutes into a run.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from pipeline import extract


class ClaudeBinaryTests(unittest.TestCase):
    def test_path_is_used_when_it_works(self):
        with mock.patch.object(extract.shutil, "which", return_value="/usr/bin/claude"):
            self.assertEqual(extract._claude_binary(), "/usr/bin/claude")

    def test_it_falls_back_when_path_is_empty(self):
        """The Scheduled Task case: PATH does not have it, the filesystem does."""
        target = Path.home() / ".local" / "bin" / "claude.exe"
        with mock.patch.object(extract.shutil, "which", return_value=None), \
             mock.patch.object(Path, "exists", lambda self: self == target):
            self.assertEqual(extract._claude_binary(), str(target))

    def test_a_missing_binary_names_the_cause(self):
        with mock.patch.object(extract.shutil, "which", return_value=None), \
             mock.patch.object(Path, "exists", lambda self: False):
            with self.assertRaises(RuntimeError) as caught:
                extract._claude_binary()
        message = str(caught.exception)
        self.assertIn("claude CLI not found", message)
        self.assertIn("Scheduled Task", message,
                      "the error must point at the environment, not just say 'not found'")

    def test_the_resolved_path_is_what_gets_executed(self):
        """A bare "claude" in argv is what produced WinError 2. It must not come back."""
        source = (Path(__file__).resolve().parents[1] / "pipeline" / "extract.py"
                  ).read_text(encoding="utf-8")
        self.assertIn('args = [_claude_binary(), "-p"', source)
        self.assertNotIn('args = ["claude", "-p"', source)


if __name__ == "__main__":
    unittest.main()
