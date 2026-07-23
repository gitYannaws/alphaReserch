import unittest

from pipeline.extract import _pack_batches


def _docs(n, chars, title=""):
    return [{"id": str(i), "raw_markdown": "x" * chars, "title": title} for i in range(n)]


class PackBatchesTests(unittest.TestCase):
    def test_char_budget_splits_before_batch_size(self):
        # 6 allowed by count, but 3x3000 already ~= 9000 > next add over 10000 budget.
        batches = list(_pack_batches(_docs(9, 3000), batch_size=6,
                                     max_batch_chars=10000, max_doc_chars=10000))
        self.assertTrue(all(sum(len(t) for _, t in b) <= 10000 for b in batches))
        self.assertTrue(all(len(b) <= 3 for b in batches))
        # No doc is lost.
        self.assertEqual(sum(len(b) for b in batches), 9)

    def test_batch_size_caps_small_docs(self):
        batches = list(_pack_batches(_docs(20, 5), batch_size=6,
                                     max_batch_chars=100000, max_doc_chars=10000))
        self.assertEqual([len(b) for b in batches], [6, 6, 6, 2])

    def test_oversized_doc_capped_and_kept(self):
        batches = list(_pack_batches(_docs(1, 50000), batch_size=6,
                                     max_batch_chars=10000, max_doc_chars=10000))
        self.assertEqual(len(batches), 1)
        doc, text = batches[0][0]
        self.assertEqual(len(text), 10000)  # capped
        # The cap is a prefix, so a span the model copies still matches the stored text.
        self.assertTrue(doc["raw_markdown"].startswith(text))

    def test_no_docs(self):
        self.assertEqual(list(_pack_batches([], 6, 10000, 10000)), [])


if __name__ == "__main__":
    unittest.main()
