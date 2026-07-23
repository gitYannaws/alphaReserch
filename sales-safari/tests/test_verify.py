import json
import tempfile
import unittest
from pathlib import Path

from pipeline import extract
from pipeline.collectors.base import Document
from pipeline.store import Store


def _mk_doc(store, run_id, key, author, body):
    url = f"https://ex.com/{key}"
    store.upsert_document(run_id, Document(
        source_type="forum", source_url=url, permalink=url, title=f"post {key}",
        raw_markdown=body, author=author, thread_url=f"https://ex.com/{key}/t"))
    return store.get_document_id_by_source_url(url)


class VerifyStageTests(unittest.TestCase):
    def _seed(self, store):
        run = "r1"
        store.start_run(run, "https://ex.com/", use_corpus=False)
        specs = [
            ("a", "alice", "I spent 3 hours fighting the export settings and gave up", "settings waste hours"),
            ("b", "bob", "white women everywhere have become impossible", "women impossible"),
            ("c", "cara", "Friday and I'm 3 martinis down lol", "martini joke"),
            ("d", "dan", "I really wish there was a tool that auto-formats these", "wants auto-format tool"),
        ]
        for key, author, body, complaint in specs:
            did = _mk_doc(store, run, key, author, body)
            store.insert_pain(run, {
                "document_id": did, "source_id": f"https://ex.com/{key}",
                "source_permalink": f"https://ex.com/{key}", "author_hash": author,
                "complaint": complaint, "workflow_pain": "", "workaround": "", "wish": "",
                "persona": "", "verbatim_span": body, "span_start": 0, "span_end": len(body),
            })
        # id -> verdict the fake judge will return
        by_span = {b: (t) for (_, _, b, _), t in zip(specs, [
            "product_friction", "social_complaint", "off_topic", "wish"])}
        return run, by_span

    def test_keep_types_policy_and_reversible_reapply(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "v.sqlite"))
            run, by_span = self._seed(store)

            def fake_call(provider, prompt, cfg):
                payload = json.loads(prompt[prompt.index("["):])
                return json.dumps([{"id": c["id"], "pain_type": by_span[c["span"]],
                                    "reason": "x"} for c in payload])

            orig = extract._call_provider
            extract._call_provider = fake_call
            try:
                r = extract.verify_run(store, run,
                                       verify_cfg={"keep_types": ["product_friction", "wish"],
                                                   "batch_size": 10})
            finally:
                extract._call_provider = orig

            self.assertEqual(r["judged"], 4)
            self.assertEqual(r["kept"], 2)
            self.assertEqual(r["rejected"], 2)
            # get_pains withholds the rejected two (social_complaint, off_topic).
            kept_spans = {p["verbatim_span"] for p in store.get_pains(run)}
            self.assertEqual(len(kept_spans), 2)
            self.assertIn("I spent 3 hours fighting the export settings and gave up", kept_spans)
            self.assertNotIn("Friday and I'm 3 martinis down lol", kept_spans)

            # Reversible knob: widen policy to include social_complaint, no LLM re-run.
            store.reapply_verify_policy(run, ["product_friction", "wish", "social_complaint"])
            self.assertEqual(len(store.get_pains(run)), 3)
            # Narrow again.
            store.reapply_verify_policy(run, ["product_friction"])
            self.assertEqual(len(store.get_pains(run)), 1)
            store.close()

    def test_missing_verdict_is_recall_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "v.sqlite"))
            run, _ = self._seed(store)

            # Judge returns nothing for any candidate.
            orig = extract._call_provider
            extract._call_provider = lambda p, pr, c: "[]"
            try:
                extract.verify_run(store, run, verify_cfg={"keep_types": ["product_friction"]})
            finally:
                extract._call_provider = orig

            # No verdict -> kept (unjudged), never silently dropped.
            self.assertEqual(len(store.get_pains(run)), 4)
            self.assertEqual(store.count_unverified_pains(run), 0)
            store.close()

    def test_get_pains_keeps_null_verified_for_old_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "v.sqlite"))
            run, _ = self._seed(store)
            # No verify run at all: every pain has verified=NULL and must still be returned.
            self.assertEqual(len(store.get_pains(run)), 4)
            store.close()


if __name__ == "__main__":
    unittest.main()
