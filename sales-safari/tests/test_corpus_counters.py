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
            store.set_run_inherited_counts(
                refresh_run,
                store.count_documents(refresh_run),
                store.count_topics(refresh_run),
                store.count_distinct_authors(refresh_run),
            )

            self.assertEqual(
                store.get_run_display_counts(refresh_run),
                {"new": 0, "threads": 0, "authors": 0},
            )

            runs = {row["job_id"]: row for row in store.list_runs(limit=10)}
            self.assertEqual(runs[refresh_run]["doc_count"], 0)
            self.assertEqual(runs[seed_run]["doc_count"], 2)

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


if __name__ == "__main__":
    unittest.main()
