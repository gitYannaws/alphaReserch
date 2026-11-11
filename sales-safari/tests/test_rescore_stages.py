"""`scripts/rescore_stages.py` re-clusters a FINISHED run in place, so it must not leave the run
holding rows from two clusterings, must not cluster pains the judge never saw, and must leave
the run in a status `scripts/guard_pipeline.py` ignores, however it ends.

Run pbfamily0001 (2026-11-10): cluster ids `<run>-<n>` are reused, and 7,371 of 8,816 ranking
rows and 12 of 21 ideas pointed at a different theme after pass 2. Pass 2 clustered 11,132
pains (41%) that lay outside pass 1's rankable slice. A SIGTERM skipped the `finally`, left
the maintenance marker and a non-terminal status, and the next clean re-score restored that
non-terminal status, which the guard relaunches.

Every test runs against a scratch ROOT with its own config.yaml and SQLite file.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np
import yaml

from pipeline.store import Store

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import rescore_stages as rs  # noqa: E402

RUN = "run1"
DIM = 8
TERMINAL = {"done", "reported", "cancelled"}
DEPENDENTS = ("rankings", "demand_scores", "soft_filters", "filter_results",
              "competitive_intel", "competitors", "competitor_reviews", "ideas", "idea_briefs")


def _axis(k, scale=10.0):
    v = np.zeros(DIM, dtype="float32")
    v[k] = scale
    return v


def _fill_dependents(store, run_id, cluster_id):
    """One row in every table keyed to a cluster id, the way stages 6-10b write them."""
    store.save_demand_score(run_id, cluster_id, {
        "pain_intensity": 5, "frequency": 5, "willingness_to_pay": 5, "reachability": 5,
        "demand_score": 5, "evidence_count": 6, "distinct_authors": 5})
    store.save_soft_filter(run_id, cluster_id, "yes", 0.9, "", [])
    store.save_filter_result(run_id, cluster_id, False, [])
    store.set_saturation(run_id, cluster_id, 2.0, 1)
    comp = store.save_competitor(run_id, cluster_id, {"name": "Rival " + run_id})
    store.save_review(run_id, comp, {"review_id": "r-" + run_id, "rating": 1, "body": "bad"})
    store.save_ranking(run_id, {"cluster_id": cluster_id, "rank": 1, "rank_score": 5.0,
                                "demand_score": 5.0, "persistence_score": 5.0,
                                "saturation_score": 2.0, "dropped": False, "filter_reasons": []})
    idea = store.save_idea(run_id, cluster_id, "Idea " + run_id, "pitch", "")
    store.save_brief(run_id, idea, cluster_id, {"problem": "p"})


def _count(store, table, run_id):
    return store.conn.execute(f"SELECT count(*) FROM {table} WHERE run_id=?",
                              (run_id,)).fetchone()[0]


class ClearClusterDependentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_clears_only_this_runs_cluster_keyed_rows(self):
        for run_id in (RUN, "run2"):
            self.store.insert_pain(run_id, {"document_id": "d", "verbatim_span": "a pain",
                                            "author_hash": "a"})
            pid = self.store.conn.execute("SELECT id FROM pains WHERE run_id=?",
                                          (run_id,)).fetchone()[0]
            self.store.save_cluster(f"{run_id}-0", run_id, "theme", 1, 1, [pid])
            _fill_dependents(self.store, run_id, f"{run_id}-0")
            self.store.save_report(run_id, "reports/x.md")
        self.assertIsNotNone(self.store.save_feedback(RUN, "theme", f"{RUN}-0", "yay"))

        counts = self.store.clear_cluster_dependents(RUN)

        self.assertEqual(counts, {table: 1 for table in DEPENDENTS})
        for table in DEPENDENTS:
            self.assertEqual(_count(self.store, table, RUN), 0, table)
            self.assertEqual(_count(self.store, table, "run2"), 1, "another run is untouched")
        for table in ("feedback", "pains", "clusters", "reports"):
            self.assertEqual(_count(self.store, table, RUN), 1, f"{table} is not a dependent")
        self.assertEqual(self.store.conn.execute(
            "SELECT count(*) FROM cluster_members WHERE cluster_id=?", (f"{RUN}-0",)).fetchone()[0], 1)


class _ScratchRoot(unittest.TestCase):
    """Three tight blobs on separate axes, 14 points: under 15, so HDBSCAN sees the raw
    vectors and the labeling is exact. A and B clear min_support; C is one loud author, so
    it forms a pass-1 theme that is NOT rankable."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "db").mkdir()
        self.db = self.tmp / "db" / "t.sqlite"
        (self.tmp / "config.yaml").write_text(yaml.safe_dump({
            "db_path": str(self.db),
            "cluster": {"min_cluster_size": 3, "min_cohesion": 0.0, "semantic_refine": False},
            "rank": {"min_support": {"evidence_count": 3, "distinct_authors": 3}},
        }), encoding="utf-8")
        self.store = Store(str(self.db))
        self.store.conn.execute("INSERT INTO runs(job_id, seed_url, stage, status) "
                                "VALUES(?, 'x', 12, 'done')", (RUN,))
        self.store.conn.commit()
        self.marker = self.tmp / "db" / f"resume-{RUN}.maintenance"
        self._cwd, self._root = os.getcwd(), rs.ROOT
        os.chdir(self.tmp)
        rs.ROOT = self.tmp
        self._handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}

    def tearDown(self):
        for s, h in self._handlers.items():
            signal.signal(s, h)
        rs.ROOT = self._root
        os.chdir(self._cwd)
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _blobs(self):
        rng = np.random.default_rng(0)
        blobs = {}
        for name, axis, n, one_author in (("A", 0, 5, False), ("B", 1, 5, False),
                                          ("C", 2, 4, True)):
            for j in range(n):
                text = f"complaint {name}{j}"
                self.store.insert_pain(RUN, {"document_id": f"{name}{j}", "verbatim_span": text,
                                             "complaint": text,
                                             "author_hash": name if one_author else f"{name}{j}"})
                pid = self.store.conn.execute("SELECT id FROM pains WHERE document_id=?",
                                              (f"{name}{j}",)).fetchone()[0]
                vec = np.asarray(_axis(axis) + rng.normal(0, 0.05, DIM), dtype="float32")
                self.store.save_embedding(RUN, pid, vec.tobytes())
                blobs.setdefault(name, []).append(pid)
        return blobs

    def _main(self, *argv):
        self.assertEqual(rs.ROOT, self.tmp, "never run the re-score against the live ROOT")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rs.main([RUN, *argv])
        return code, out.getvalue()

    def _run(self):
        self.store.conn.commit()
        return self.store.get_run(RUN)

    def _members(self):
        return {r[0] for r in self.store.conn.execute(
            "SELECT cm.pain_id FROM cluster_members cm JOIN clusters c ON c.id = cm.cluster_id "
            "WHERE c.run_id=?", (RUN,))}


class ReclusterLeavesNoStaleRowsTests(_ScratchRoot):
    def test_a_cluster_step_clears_rows_keyed_to_the_old_themes(self):
        self._blobs()
        _fill_dependents(self.store, RUN, f"{RUN}-0")

        code, out = self._main("--from", "cluster1", "--to", "cluster1")

        self.assertEqual(code, 0, out)
        self.assertEqual({t: _count(self.store, t, RUN) for t in DEPENDENTS},
                         {t: 0 for t in DEPENDENTS})
        self.assertIn("cleared stale rows: {", out)
        self.assertIn("db/baseline/", out, "the log must say where the deleted rows survive")


class PassTwoSeesOnlyTheJudgedSliceTests(_ScratchRoot):
    def setUp(self):
        super().setUp()
        self.blobs = self._blobs()
        self.rejected = self.blobs["A"][0]
        self.judged = []

        def fake_verify(store, run_id, verify_cfg, extract_cfg, progress=None, pain_ids=None):
            self.judged.extend(pain_ids)
            for pid in pain_ids:
                store.set_pain_verdict(pid, "pain", 0 if pid == self.rejected else 1, "test")
            return {"candidates": len(pain_ids), "judged": len(pain_ids)}

        self._verify, rs.verify_run = rs.verify_run, fake_verify

    def tearDown(self):
        rs.verify_run = self._verify
        super().tearDown()

    def test_pass_two_clusters_only_kept_pains_from_the_pass_one_slice(self):
        code, out = self._main("--from", "cluster1", "--to", "cluster2")

        self.assertEqual(code, 0, out)
        slice_ids = set(self.blobs["A"] + self.blobs["B"])
        self.assertEqual(set(self.judged), slice_ids, "the judge sees pass 1's rankable slice")
        members = self._members()
        self.assertEqual(members & set(self.blobs["C"]), set(),
                         "a pain outside the pass-1 slice was never judged")
        self.assertNotIn(self.rejected, members, "a rejected pain stays out")
        self.assertEqual(members, slice_ids - {self.rejected})

    def test_the_slice_survives_into_a_resumed_run(self):
        self._main("--from", "cluster1", "--to", "cluster1")
        saved = json.loads((self.tmp / "db" / f"rescore-{RUN}.slice.json").read_text())
        self.assertEqual(set(saved["pain_ids"]), set(self.blobs["A"] + self.blobs["B"]))
        self.assertEqual(list((self.tmp / "db").glob("*.tmp")), [], "written by temp + rename")

        code, out = self._main("--from", "verify", "--to", "cluster2")

        self.assertEqual(code, 0, out)
        self.assertEqual(self._members() & set(self.blobs["C"]), set())

    def test_resuming_without_the_slice_file_refuses_and_names_cluster1(self):
        self._main("--from", "cluster1", "--to", "cluster1")
        (self.tmp / "db" / f"rescore-{RUN}.slice.json").unlink()

        before = self._run()

        code, out = self._main("--from", "verify")

        self.assertNotEqual(code, 0)
        self.assertIn("--from cluster1", out)
        self.assertEqual(self.judged, [], "nothing was judged")
        self.assertEqual(self._run(), before)

    def test_slice_pains_without_a_verdict_are_named_before_pass_two(self):
        failed_batch, omitted = self.blobs["B"][:2]
        verify = rs.verify_run

        def judge_with_gaps(store, run_id, verify_cfg, extract_cfg, progress=None,
                            pain_ids=None):
            r = verify(store, run_id, verify_cfg, extract_cfg, progress,
                       [p for p in pain_ids if p != failed_batch])
            store.set_pain_verdict(omitted, "unjudged", 1, "no verdict")
            return r

        rs.verify_run = judge_with_gaps
        code, out = self._main("--from", "cluster1", "--to", "cluster2")

        self.assertEqual(code, 0, out)
        self.assertIn("WARN cluster2: of 10 slice pains, 1 were never judged (--from verify "
                      "judges them) and 1 came back from the judge without a verdict", out)

    def test_a_failed_slice_write_keeps_the_previous_slice(self):
        path = self.tmp / "db" / f"rescore-{RUN}.slice.json"
        previous = json.dumps({"run_id": RUN, "created_at": "earlier", "pain_ids": ["p"]})
        path.write_text(previous)

        def no_rename(src, dst):
            raise OSError("disk full")

        real, rs.os.replace = rs.os.replace, no_rename
        try:
            with self.assertRaises(OSError):
                self._main("--from", "cluster1", "--to", "cluster1")
        finally:
            rs.os.replace = real
        self.assertEqual(path.read_text(), previous)


class LeavesTheRunTerminalTests(_ScratchRoot):
    """The guard relaunches the whole pipeline for any status outside
    {done, reported, cancelled} that has no maintenance marker."""

    CHILD = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "repo, root, how = sys.argv[1], Path(sys.argv[2]), sys.argv[3]\n"
        "sys.path[:0] = [repo, repo + '/scripts']\n"
        "import rescore_stages as rs\n"
        "rs.ROOT = root\n"
        "def slow_rank(store, run_id, cfg):\n"
        "    if how == 'ranked':\n"
        "        store.set_stage(run_id, 9, 'ranked')\n"
        "    print('RANK-STARTED', flush=True)\n"
        "    time.sleep(60)\n"
        "rs._rank = slow_rank\n"
        "sys.exit(rs.main(['run1', '--from', 'rank', '--to', 'rank']))\n"
    )

    def _kill_mid_step(self, sig, how="ranked"):
        proc = subprocess.Popen([sys.executable, "-c", self.CHILD, str(REPO), str(self.tmp), how],
                                cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        watchdog = threading.Timer(90, proc.kill)
        watchdog.start()
        try:
            seen = []
            for line in proc.stdout:
                seen.append(line)
                if "RANK-STARTED" in line:
                    break
            self.assertIn("RANK-STARTED\n", seen, "".join(seen))
            self.assertTrue(self.marker.exists(), "the marker holds the guard off mid-run")
            proc.send_signal(sig)
            rest, _ = proc.communicate(timeout=60)
        finally:
            watchdog.cancel()
        return proc.returncode, "".join(seen) + rest

    def test_a_kill_leaves_a_terminal_status_and_says_where_to_resume(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                self.store.set_stage(RUN, 12, "done")
                code, out = self._kill_mid_step(sig)
                run = self._run()
                self.assertNotEqual(code, 0, out)
                self.assertFalse(self.marker.exists(), "marker left behind:\n" + out)
                self.assertIn(run["status"], TERMINAL)
                self.assertEqual(run["status"], "cancelled", "done would hide the mixed state")
                self.assertIn("--from rank", run["error"])
                self.assertIn("NOTIFY", out)
                self.assertIn("--from rank", out)

    def test_a_sigkill_is_recoverable(self):
        """SIGKILL runs no handler. While a step runs, the run is parked `cancelled`; once a
        stage has written its own status, the marker still holds the resume point."""
        for how, during in (("quiet", "cancelled"), ("ranked", "ranked")):
            with self.subTest(how=how):
                self.store.set_stage(RUN, 12, "done")
                self._kill_mid_step(signal.SIGKILL, how)
                self.assertEqual(self._run()["status"], during)
                self.assertIn("[restore: 12 done rank]", self.marker.read_text())

                code, out = self._main("--from", "rank")

                self.assertEqual(code, 0, out)
                self.assertIn("(it stopped in step rank)", out)
                run = self._run()
                self.assertEqual((run["stage"], run["status"], run["error"]), (12, "done", None))
                self.assertFalse(self.marker.exists())

    def test_the_marker_stays_when_no_terminal_status_can_be_written(self):
        def rank_then_lock(store, run_id, cfg):
            def locked(*a):
                raise sqlite3.OperationalError("database is locked")
            store.set_stage(run_id, 9, "ranked")
            store.set_stage = store.cancel_run = locked
            return {}

        real, rs._rank = rs._rank, rank_then_lock
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self._main("--from", "rank")
        finally:
            rs._rank = real
        self.assertEqual(self._run()["status"], "ranked")
        self.assertIn("[restore: 12 done rank]", self.marker.read_text(),
                      "the guard stays off and the next start can resume")

    def test_a_partial_re_score_is_not_given_back_its_old_status(self):
        code, out = self._main("--from", "demand", "--to", "demand")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._run()["status"], "cancelled", "--to stopped short of rank")
        self.assertIn("--from rank", self._run()["error"])

        self.store.cancel_run(RUN, 12, "stopped [restore: 12 done softfilter]")
        code, out = self._main("--from", "rank")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._run()["status"], "cancelled", "softfilter never finished")
        self.assertIn("--from softfilter", self._run()["error"])

    def test_the_next_clean_run_restores_the_status_from_before_the_kill(self):
        self._kill_mid_step(signal.SIGTERM)

        code, out = self._main("--from", "rank")

        self.assertEqual(code, 0, out)
        run = self._run()
        self.assertEqual((run["stage"], run["status"], run["error"]), (12, "done", None))

    def test_a_failed_step_is_not_reported_as_done(self):
        def broken_rank(store, run_id, cfg):
            store.set_stage(run_id, 9, "ranked")
            raise RuntimeError("judge offline")

        real, rs._rank = rs._rank, broken_rank
        try:
            with self.assertRaises(RuntimeError):
                self._main("--from", "rank")
        finally:
            rs._rank = real
        run = self._run()
        self.assertEqual(run["status"], "cancelled")
        self.assertIn("--from rank", run["error"])
        self.assertFalse(self.marker.exists())

    def test_an_unfinished_run_is_refused_before_anything_changes(self):
        self.store.set_stage(RUN, 9, "ranked")

        code, out = self._main("--from", "rank")

        self.assertNotEqual(code, 0)
        self.assertEqual((self._run()["stage"], self._run()["status"]), (9, "ranked"))
        self.assertFalse(self.marker.exists())


class SoftfilterCountTests(_ScratchRoot):
    def test_missing_evidence_is_logged(self):
        real = rs.softfilter_run
        rs.softfilter_run = lambda *a, **kw: {"classified": 5, "evidence_missing": 2}
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rs._softfilter(self.store, RUN, {})
        finally:
            rs.softfilter_run = real
        self.assertIn("WARN softfilter: 2 of 5 verdicts came without evidence", out.getvalue())


class MaintenanceMarkerTests(_ScratchRoot):
    def test_the_marker_names_the_owner_pid(self):
        def peek(store, run_id, cfg):
            self.seen = self.marker.read_text(encoding="utf-8")
            return {}

        real, rs._rank = rs._rank, peek
        try:
            self._main("--from", "rank")
        finally:
            rs._rank = real
        self.assertIn(f"pid={os.getpid()}", self.seen)

    def test_a_marker_held_by_a_live_process_refuses_to_start(self):
        self.marker.write_text(f"rescore_stages pid={os.getpid()}", encoding="utf-8")

        code, out = self._main("--from", "rank")

        self.assertNotEqual(code, 0)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), f"rescore_stages pid={os.getpid()}")
        self.assertEqual(self._run()["status"], "done")

    def test_a_marker_without_a_pid_counts_as_live(self):
        self.marker.write_text("verify backfill over the ranked slice", encoding="utf-8")

        code, out = self._main("--from", "rank")

        self.assertNotEqual(code, 0)
        self.assertTrue(self.marker.exists())

    def test_a_marker_left_by_a_dead_process_is_replaced(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.marker.write_text(f"rescore_stages pid={gone.pid}", encoding="utf-8")

        code, out = self._main("--from", "rank")

        self.assertEqual(code, 0, out)
        self.assertIn(f"pid {gone.pid} is gone", out)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
