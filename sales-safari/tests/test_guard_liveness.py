"""The guard must never infer "dead" from "I could not see it".

This check has been wrong twice, in two different ways, and both cost hours.

2026-08-21: it piped Win32_Process CommandLine strings back to Python. PowerShell runs those
through the console formatter, which TRUNCATES at the host buffer width -- 70 columns on this
machine -- so a ~105-character resume command line arrived as "...python.exe -", with
`pipeline.resume` and the job id cut clean off. Every healthy runner read as dead.
pbfamily0001 was restarted every 10 minutes for six hours, stacking concurrent runners onto
one SQLite file; stage 5 sat at 2/3 for three hours and stage 7 restarted 10 times.

2026-08-23: matching inside PowerShell fixed the truncation and did not fix the check. The
Scheduled Task's own processes came back with a NULL CommandLine -- not readable from that
session at all -- so `-like` matched nothing, the count was 0, and the guard restarted a live
runner again. Two runners, "database is locked", a 199-second Claude call lost.

So the rule under test is a policy, not an implementation: every unknown resolves to ALIVE.
"""
from __future__ import annotations

import unittest
from unittest import mock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import guard_pipeline as guard  # noqa: E402

JOB = "pbfamily0001"
TASK = "AlphaPassportFamilyPipeline"


def _responses(state=None, pid_count=None, cmdline_count=None, unreadable_count=None):
    """Fake _ps: route by what the script is asking for. None means PowerShell failed."""
    def fake(script):
        if "Get-ScheduledTask" in script:
            return state
        if "Get-Process -Id" in script:
            return pid_count
        if "pipeline.resume" in script:
            return cmdline_count
        if "-not $_.CommandLine" in script:
            return unreadable_count
        raise AssertionError("unexpected script: %s" % script)
    return fake


class LivenessTests(unittest.TestCase):
    def _alive(self, **kw):
        with mock.patch.object(guard, "_ps", _responses(**kw)), \
             mock.patch.object(guard, "log"):
            return guard.process_alive(JOB, TASK)

    def test_the_task_state_is_believed_first(self):
        self.assertTrue(self._alive(state="Running"))

    def test_a_ready_task_with_nothing_else_running_is_dead(self):
        self.assertFalse(self._alive(state="Ready"))

    def test_an_unreadable_command_line_means_alive(self):
        """The 2026-08-23 failure: the runner was there, this session just could not see it."""
        self.assertTrue(self._alive(state=None, pid_count="0", cmdline_count="0",
                                    unreadable_count="2"))

    def test_the_pidfile_is_trusted_when_the_task_state_is_unavailable(self):
        self.assertTrue(self._alive(state=None, pid_count="1"))

    def test_the_command_line_match_still_works_as_a_backstop(self):
        self.assertTrue(self._alive(state=None, pid_count="0", cmdline_count="1"))

    def test_powershell_failing_entirely_means_alive(self):
        """A guard that reads its own failure as "the job died" is the whole bug."""
        self.assertTrue(self._alive(state=None, pid_count=None, cmdline_count=None,
                                    unreadable_count=None))

    def test_everything_clear_and_readable_means_dead(self):
        self.assertFalse(self._alive(state=None, pid_count="0", cmdline_count="0",
                                     unreadable_count="0"))

    def test_a_job_id_is_never_interpolated_unchecked(self):
        """The id is pasted into a PowerShell string, so it is validated, not escaped."""
        with self.assertRaises(ValueError):
            guard.process_alive("pb'; Remove-Item C:\\ -Recurse; '", TASK)

    def test_a_task_name_is_validated_too(self):
        with mock.patch.object(guard, "_ps") as ps:
            self.assertIsNone(guard._task_is_running("evil'; rm -rf /; '"))
            ps.assert_not_called()


class RestartPolicyTests(unittest.TestCase):
    def test_terminal_states_are_never_revived(self):
        self.assertEqual(guard.TERMINAL, {"done", "reported", "cancelled"})

    def test_a_backoff_ceiling_exists(self):
        """A run that keeps dying instantly is a real fault, not a session blip."""
        self.assertGreater(guard.MAX_RESTARTS_PER_HOUR, 0)
        self.assertLessEqual(guard.MAX_RESTARTS_PER_HOUR, 6)


if __name__ == "__main__":
    unittest.main()
