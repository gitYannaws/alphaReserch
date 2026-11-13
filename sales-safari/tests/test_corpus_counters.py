import tempfile
import unittest
from pathlib import Path

from pipeline.collectors.base import Document
from pipeline.store import Store


class CorpusCounterTests(unittest.TestCase):
    def test_corpus_refresh_display_counts_start_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "safari.sqlite")
            store = Store(db_path)

            seed_run = "seedrun"
            store.start_run(seed_run, "https://www.reddit.com/r/test/", use_corpus=True)
            docs = [
                Document(
                    source_type="forum",
                    source_url="https://www.reddit.com/r/test/comments/a/thread/1",
                    permalink="https://www.reddit.com/r/test/comments/a/thread/1",
                    title="thread a",
                    raw_markdown="alpha",
                    author="alice",
                    thread_url="https://www.reddit.com/r/test/comments/a/thread/",
                ),
                Document(
                    source_type="forum",
                    source_url="https://www.reddit.com/r/test/comments/b/thread/1",
                    permalink="https://www.reddit.com/r/test/comments/b/thread/1",
                    title="thread b",
                    raw_markdown="beta",
                    author="bob",
                    thread_url="https://www.reddit.com/r/test/comments/b/thread/",
                ),
            ]
            for doc in docs:
                store.upsert_document(seed_run, doc)
                did = store.get_document_id_by_source_url(doc.source_url)
                store.link_document_to_corpus("reddit:r/test", did, doc.fetched_at)

            refresh_run = "refreshrun"
            store.start_run(refresh_run, "https://www.reddit.com/r/test/", use_corpus=True)
            store.link_run_to_corpus(refresh_run, "reddit:r/test")
            store.ensure_run_inherited_counts(refresh_run)

            self.assertEqual(
                store.get_run_display_counts(refresh_run),
                {"new": 0, "threads": 0, "authors": 0},
            )

            runs = {row["job_id"]: row for row in store.list_runs(limit=10)}
            self.assertEqual(runs[refresh_run]["doc_count"], 0)
            self.assertEqual(runs[seed_run]["doc_count"], 2)

            store.close()

    def test_resume_does_not_rebaseline_work_already_collected(self):
        """A resumed run must keep the baseline taken when it first started.

        Re-snapshotting on resume folds everything the run collected before the
        interruption into `inherited`, permanently reporting 0 new.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "safari.sqlite"))

            def _doc(thread: str, n: int, author: str) -> Document:
                url = f"https://www.reddit.com/r/test/comments/{thread}/thread/{n}"
                return Document(
                    source_type="forum", source_url=url, permalink=url,
                    title=f"thread {thread}", raw_markdown=f"body {thread}{n}",
                    author=author,
                    thread_url=f"https://www.reddit.com/r/test/comments/{thread}/thread/",
                )

            def _collect(run_id: str, doc: Document):
                store.upsert_document(run_id, doc)
                did = store.get_document_id_by_source_url(doc.source_url)
                store.link_document_to_corpus("reddit:r/test", did, doc.fetched_at)

            seed_run = "seedrun"
            store.start_run(seed_run, "https://www.reddit.com/r/test/", use_corpus=True)
            store.link_run_to_corpus(seed_run, "reddit:r/test")
            store.ensure_run_inherited_counts(seed_run)
            _collect(seed_run, _doc("a", 1, "alice"))

            # Second run starts against a 1-doc corpus, collects one new doc, dies.
            resumed = "resumedrun"
            store.start_run(resumed, "https://www.reddit.com/r/test/", use_corpus=True)
            store.link_run_to_corpus(resumed, "reddit:r/test")
            self.assertEqual(
                store.ensure_run_inherited_counts(resumed),
                {"docs": 1, "threads": 1, "authors": 1},
            )
            _collect(resumed, _doc("b", 1, "bob"))
            self.assertEqual(
                store.get_run_display_counts(resumed),
                {"new": 1, "threads": 1, "authors": 1},
            )

            # Resume re-links the corpus and re-runs the baseline call: the already
            # collected doc must still count as new, not be absorbed into the baseline.
            store.link_run_to_corpus(resumed, "reddit:r/test")
            self.assertEqual(
                store.ensure_run_inherited_counts(resumed),
                {"docs": 1, "threads": 1, "authors": 1},
            )
            _collect(resumed, _doc("c", 1, "carol"))

            self.assertEqual(
                store.get_run_display_counts(resumed),
                {"new": 2, "threads": 2, "authors": 2},
            )
            runs = {row["job_id"]: row for row in store.list_runs(limit=10)}
            self.assertEqual(runs[resumed]["doc_count"], 2)

            store.close()

    def test_first_run_on_empty_corpus_keeps_its_zero_baseline(self):
        """An explicit 0 baseline is real and must not be re-taken on resume."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "safari.sqlite"))
            url = "https://www.reddit.com/r/test/comments/a/thread/1"
            doc = Document(
                source_type="forum", source_url=url, permalink=url, title="thread a",
                raw_markdown="alpha", author="alice",
                thread_url="https://www.reddit.com/r/test/comments/a/thread/",
            )

            run = "firstrun"
            store.start_run(run, "https://www.reddit.com/r/test/", use_corpus=True)
            store.link_run_to_corpus(run, "reddit:r/test")
            self.assertEqual(
                store.ensure_run_inherited_counts(run),
                {"docs": 0, "threads": 0, "authors": 0},
            )
            store.upsert_document(run, doc)
            did = store.get_document_id_by_source_url(doc.source_url)
            store.link_document_to_corpus("reddit:r/test", did, doc.fetched_at)

            self.assertEqual(
                store.ensure_run_inherited_counts(run),
                {"docs": 0, "threads": 0, "authors": 0},
            )
            self.assertEqual(
                store.get_run_display_counts(run),
                {"new": 1, "threads": 1, "authors": 1},
            )
            store.close()

    def test_last_topic_found_at_uses_latest_first_seen_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "safari.sqlite")
            store = Store(db_path)

            run_id = "topicrun"
            store.start_run(run_id, "https://www.reddit.com/r/test/")
            docs = [
                Document(
                    source_type="forum",
                    source_url="https://www.reddit.com/r/test/comments/a/thread/op",
                    permalink="https://www.reddit.com/r/test/comments/a/thread/op",
                    title="thread a",
                    raw_markdown="alpha",
                    author="alice",
                    thread_url="https://www.reddit.com/r/test/comments/a/thread/",
                    fetched_at="2026-07-13T12:00:00+00:00",
                ),
                Document(
                    source_type="forum",
                    source_url="https://www.reddit.com/r/test/comments/a/thread/reply",
                    permalink="https://www.reddit.com/r/test/comments/a/thread/reply",
                    title="thread a",
                    raw_markdown="alpha reply",
                    author="bob",
                    thread_url="https://www.reddit.com/r/test/comments/a/thread/",
                    fetched_at="2026-07-13T12:05:00+00:00",
                ),
                Document(
                    source_type="forum",
                    source_url="https://www.reddit.com/r/test/comments/b/thread/op",
                    permalink="https://www.reddit.com/r/test/comments/b/thread/op",
                    title="thread b",
                    raw_markdown="beta",
                    author="cara",
                    thread_url="https://www.reddit.com/r/test/comments/b/thread/",
                    fetched_at="2026-07-13T12:10:00+00:00",
                ),
            ]
            for doc in docs:
                store.upsert_document(run_id, doc)

            self.assertEqual(
                store.get_last_topic_found_at(run_id),
                "2026-07-13T12:10:00+00:00",
            )

            store.set_last_topic_found_at(run_id, "2026-07-13T12:12:00+00:00")
            self.assertEqual(
                store.get_last_topic_found_at(run_id),
                "2026-07-13T12:12:00+00:00",
            )

            store.close()

    def test_freshness_batch_matches_the_run_documents_join(self):
        """list_sources reads run_corpora instead of re-deriving run<->corpus from the
        run_documents x corpus_documents join. The two must agree, or the Sources page
        starts reporting a different last run than the corpus actually had."""
        JOIN = (
            "SELECT r.job_id, r.created_at, r.inherited_doc_count FROM runs r "
            "JOIN run_documents rd ON rd.run_id=r.job_id "
            "JOIN corpus_documents cd ON cd.document_id=rd.document_id "
            "WHERE cd.corpus_key=? "
            "GROUP BY r.job_id, r.created_at, r.inherited_doc_count "
            "ORDER BY r.created_at DESC LIMIT 1"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "safari.sqlite"))
            key = "reddit:r/test"
            store.ensure_corpus(key, "https://www.reddit.com/r/test/")

            for i, run_id in enumerate(["runold", "runmid", "runnew"]):
                store.start_run(run_id, "https://www.reddit.com/r/test/", use_corpus=True)
                doc = Document(
                    source_type="forum",
                    source_url=f"https://www.reddit.com/r/test/comments/{i}/t/1",
                    permalink=f"https://www.reddit.com/r/test/comments/{i}/t/1",
                    title=f"thread {i}",
                    raw_markdown="body",
                    author=f"user{i}",
                    thread_url=f"https://www.reddit.com/r/test/comments/{i}/t/",
                )
                store.upsert_document(run_id, doc)
                did = store.get_document_id_by_source_url(doc.source_url)
                store.link_document_to_corpus(key, did, doc.fetched_at)
                store.link_run_to_corpus(run_id, key)

            expected = store.conn.execute(JOIN, (key,)).fetchone()
            self.assertIsNotNone(expected, "join should find a run")
            self.assertEqual(store._latest_run_per_corpus((key,)).get(key), tuple(expected))

            # and the page-level call agrees with the per-corpus one
            store.mark_corpus_backfilled(key)
            self.assertTrue(
                store.add_source("src1", "https://www.reddit.com/r/test/", "test", key))
            row = next(s for s in store.list_sources() if s["corpus_key"] == key)
            self.assertEqual(row["freshness"], store.get_corpus_freshness(key)["status"])
            store.close()


if __name__ == "__main__":
    unittest.main()
