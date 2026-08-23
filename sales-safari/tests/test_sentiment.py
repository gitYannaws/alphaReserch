"""Stage 7.6 sentiment: batching, resume, bad output, and the chart's summary query."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import s7c_sentiment as sent   # noqa: E402
from pipeline.store import Store             # noqa: E402


def _seed(store, run_id="r1", n_themes=3):
    c = store.conn
    c.execute("INSERT OR REPLACE INTO runs (job_id,seed_url,stage,status,created_at) "
              "VALUES (?,?,?,?,?)", (run_id, "seed", 7, "done", "2026-01-01T00:00:00Z"))
    for i in range(n_themes):
        cid = "c%d" % i
        c.execute("INSERT INTO clusters (id,run_id,label,size,distinct_authors) "
                  "VALUES (?,?,?,?,?)", (cid, run_id, "theme %d" % i, 1, i + 1))
        pid = "p%d" % i
        c.execute(
            "INSERT INTO pains (id,run_id,document_id,source_id,author_hash,complaint,"
            "workflow_pain,workaround,wish,persona,verbatim_span,source_permalink,"
            "created_at,persona_canonical,verified) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (pid, run_id, None, "src", "a%d" % i, "it is slow", "", "", "", "user",
             "I have given up on this", "http://x/%s" % pid, "2026-01-01T00:00:00Z", "user"))
        c.execute("INSERT INTO cluster_members (cluster_id,pain_id) VALUES (?,?)", (cid, pid))
    c.commit()


def _reply(items):
    """A fake _call_extractor returning one JSON array."""
    import json
    return lambda prompt, cfg=None: (json.dumps(items), "fake")


class SentimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))
        _seed(self.store)

    def tearDown(self):
        self.store.close()

    def test_saves_one_row_per_theme(self):
        reply = _reply([{"id": "c%d" % i, "stance": "angry", "urgency": 8, "reason": "loud"}
                        for i in range(3)])
        with mock.patch.object(sent, "_call_extractor", reply):
            out = sent.sentiment_run(self.store, "r1")
        self.assertEqual(out["classified"], 3)
        self.assertEqual(out["counts"], {"angry": 3})
        self.assertEqual(len(self.store.get_sentiment_ids("r1")), 3)

    def test_a_theme_the_model_skipped_is_still_recorded(self):
        """Otherwise a skipped theme is indistinguishable from one never sent, and the
        resume below would ask for it forever."""
        with mock.patch.object(sent, "_call_extractor",
                               _reply([{"id": "c0", "stance": "angry", "urgency": 9}])):
            out = sent.sentiment_run(self.store, "r1")
        self.assertEqual(out["classified"], 1)
        self.assertEqual(len(self.store.get_sentiment_ids("r1")), 3)
        stances = {r["stance"] for r in self.store.get_sentiment_summary("r1")}
        self.assertIn("unrated", stances)

    def test_resume_only_sends_the_unrated(self):
        self.store.save_sentiment("r1", "c0", "angry", 9.0, "")
        seen = {}

        def spy(prompt, cfg=None):
            seen["prompt"] = prompt
            return '[{"id":"c1","stance":"seeking","urgency":10}]', "fake"

        with mock.patch.object(sent, "_call_extractor", spy):
            sent.sentiment_run(self.store, "r1")
        self.assertNotIn('"c0"', seen["prompt"])
        self.assertIn('"c1"', seen["prompt"])

    def test_a_failed_batch_does_not_kill_the_pass(self):
        def boom(prompt, cfg=None):
            raise RuntimeError("model returned garbage")

        with mock.patch.object(sent, "_call_extractor", boom):
            out = sent.sentiment_run(self.store, "r1")
        self.assertEqual(out["classified"], 0)
        self.assertEqual(len(self.store.get_sentiment_ids("r1")), 3)   # rows still written

    def test_an_invented_stance_falls_back_to_neutral(self):
        with mock.patch.object(sent, "_call_extractor",
                               _reply([{"id": "c0", "stance": "ecstatic", "urgency": 99},
                                       {"id": "c1", "stance": "ANGRY", "urgency": -4},
                                       {"id": "c2", "stance": "seeking", "urgency": "x"}])):
            sent.sentiment_run(self.store, "r1")
        got = {r["stance"]: r for r in self.store.get_sentiment_summary("r1")}
        self.assertIn("neutral", got)          # "ecstatic" is not a label
        self.assertIn("angry", got)            # case is normalised
        self.assertIn("seeking", got)          # a non-numeric urgency is not fatal

    def test_urgency_is_clamped_to_the_stated_range(self):
        with mock.patch.object(sent, "_call_extractor",
                               _reply([{"id": "c0", "stance": "angry", "urgency": 99}])):
            sent.sentiment_run(self.store, "r1")
        row = [r for r in self.store.get_sentiment_summary("r1") if r["stance"] == "angry"][0]
        self.assertEqual(row["urgency"], 10.0)

    def test_summary_weights_by_authors_not_by_theme_count(self):
        """c2 has 3 authors, c0 and c1 have 1 and 2. Theme counts would say 2 v 1."""
        self.store.save_sentiment("r1", "c0", "resigned", 1.0, "")
        self.store.save_sentiment("r1", "c1", "resigned", 1.0, "")
        self.store.save_sentiment("r1", "c2", "seeking", 9.0, "")
        got = {r["stance"]: r for r in self.store.get_sentiment_summary("r1")}
        self.assertEqual((got["resigned"]["themes"], got["resigned"]["authors"]), (2, 3))
        self.assertEqual((got["seeking"]["themes"], got["seeking"]["authors"]), (1, 3))

    def test_batches_are_bounded_by_characters(self):
        items = [{"id": "c%d" % i, "label": "x" * 400, "pains": []} for i in range(10)]
        sizes = [len(b) for b in sent._chunks(items, 40, 1000)]
        self.assertTrue(all(s <= 3 for s in sizes), sizes)
        self.assertEqual(sum(sizes), 10)

    def test_payload_prefers_the_verbatim_quote(self):
        """The extractor's `complaint` is a flattened paraphrase with the heat removed."""
        cluster = self.store.get_cluster_details("r1", cluster_ids=["c0"])[0]
        self.assertEqual(sent._theme_payload(cluster)["pains"],
                         ["I have given up on this"])


class IdeaStrengthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))
        _seed(self.store)
        c = self.store.conn
        c.execute("INSERT INTO ideas (id,run_id,cluster_id,title,pitch,created_at) "
                  "VALUES (?,?,?,?,?,?)", ("i1", "r1", "c2", "Big idea", "p", "2026-01-01"))
        c.execute("INSERT INTO ideas (id,run_id,cluster_id,title,pitch,created_at) "
                  "VALUES (?,?,?,?,?,?)", ("i2", "r1", "c0", "Thin idea", "p", "2026-01-01"))
        c.execute("INSERT INTO demand_scores (cluster_id,run_id,pain_intensity,frequency,"
                  "willingness_to_pay,reachability,demand_score,evidence_count,distinct_authors)"
                  " VALUES (?,?,?,?,?,?,?,?,?)", ("c2", "r1", 5, 5, 5, 5, 8.0, 1, 3))
        c.execute("INSERT INTO demand_scores (cluster_id,run_id,pain_intensity,frequency,"
                  "willingness_to_pay,reachability,demand_score,evidence_count,distinct_authors)"
                  " VALUES (?,?,?,?,?,?,?,?,?)", ("c0", "r1", 1, 1, 1, 1, 2.0, 1, 1))
        c.commit()

    def tearDown(self):
        self.store.close()

    def test_ideas_carry_their_theme_evidence_strongest_first(self):
        rows = self.store.get_idea_strength("r1")
        self.assertEqual([r["title"] for r in rows], ["Big idea", "Thin idea"])
        self.assertEqual(rows[0]["authors"], 3)
        self.assertEqual(rows[1]["authors"], 1)

    def test_an_idea_with_no_theme_still_appears(self):
        self.store.conn.execute(
            "INSERT INTO ideas (id,run_id,cluster_id,title,pitch,created_at) "
            "VALUES (?,?,?,?,?,?)", ("i3", "r1", "gone", "Orphan", "p", "2026-01-01"))
        self.store.conn.commit()
        rows = self.store.get_idea_strength("r1")
        orphan = [r for r in rows if r["title"] == "Orphan"][0]
        self.assertEqual((orphan["authors"], orphan["demand"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
