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

    def test_required_verdict_blocks_unjudged_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "v.sqlite"))
            run, _ = self._seed(store)

            orig = extract._call_provider
            extract._call_provider = lambda p, pr, c: "[]"
            try:
                with self.assertRaisesRegex(RuntimeError, "requires a verdict"):
                    extract.verify_run(
                        store, run,
                        verify_cfg={"keep_types": ["product_friction"],
                                    "require_verdict": True},
                    )
            finally:
                extract._call_provider = orig

            # Keeping these rows NULL makes the failed verification stage resumable and
            # prevents a caller configured for precision from treating them as approved.
            self.assertEqual(store.count_unverified_pains(run), 4)
            store.close()

    def test_verifier_uses_configured_provider_fallback_chain(self):
        calls = []

        def fake_call(prompt, cfg):
            calls.append(cfg["providers"])
            return json.dumps([
                {"id": "p1", "pain_type": "product_friction", "reason": "manual work"}
            ]), "codex"

        orig = extract._call_extractor
        extract._call_extractor = fake_call
        try:
            verdicts = extract.classify_candidates(
                [{"id": "p1", "span": "I retype invoices every week.", "context": ""}],
                verify_cfg={"providers": ["claude", "codex"],
                            "keep_types": ["product_friction"]},
            )
        finally:
            extract._call_extractor = orig

        self.assertEqual(calls, [["claude", "codex"]])
        self.assertEqual(verdicts["p1"]["verified"], 1)

    def test_rejected_pain_embeddings_are_not_cluster_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "v.sqlite"))
            run, _ = self._seed(store)
            pains = store.get_pains(run)
            for i, pain in enumerate(pains):
                store.save_embedding(run, pain["id"], bytes([i]))
            store.set_pain_verdict(pains[0]["id"], "off_topic", 0, "joke")

            embedded_ids = {row[0] for row in store.get_embeddings(run)}
            self.assertEqual(embedded_ids, {p["id"] for p in pains[1:]})
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
