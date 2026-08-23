"""The brief's quote gate must reject invented quotes without rejecting real excerpts.

It exists so a wedge traces to something a real reviewer actually wrote. It used to test
whole-string equality against the review body, which is a much harsher rule: quote the
ENTIRE review or be discarded. Review bodies on run pbfamily0001 ran a median of 125
characters and 521 at the 90th percentile, so a model excerpting the damning sentence --
precisely what a wedge should cite -- failed every time. Result: 1 of 21 briefs carried
review evidence despite 188 mined reviews, and the lone survivor was a 128-character review
short enough to echo whole.

Containment is the right test. A contiguous substring of a supplied body is still verbatim
and still un-inventable, which is the only property that matters here.
"""
from __future__ import annotations

import unittest

from pipeline.s10b_brief import MIN_QUOTE_CHARS, _clean_incumbents, _norm

REVIEW = ("Airbnb is a scam. They charged me more than the actual booking amount, "
          "and now they're refusing to refund the overcharged amount, which is theft.")
INDEX = {_norm(REVIEW): {"rating": 1, "source_url": "https://example.test/r/1",
                         "competitor": "Airbnb"}}
SUPPLIED = set(INDEX)


def _clean(quote):
    return _clean_incumbents([{"name": "Airbnb", "fails_at": "refunds", "quote": quote}],
                             SUPPLIED, INDEX)


class QuoteGateTests(unittest.TestCase):
    def test_a_verbatim_excerpt_is_kept_and_cited(self):
        out, kept = _clean("They charged me more than the actual booking amount")
        self.assertEqual(kept, 1)
        self.assertEqual(out[0]["rating"], 1)
        self.assertEqual(out[0]["source_url"], "https://example.test/r/1",
                         "a kept quote must carry its real citation")

    def test_the_whole_body_still_matches(self):
        _out, kept = _clean(REVIEW)
        self.assertEqual(kept, 1)

    def test_case_and_whitespace_differences_do_not_break_the_match(self):
        _out, kept = _clean("  THEY  CHARGED me MORE than the actual booking amount ")
        self.assertEqual(kept, 1)

    def test_an_invented_quote_is_dropped_but_the_incumbent_survives(self):
        out, kept = _clean("Airbnb refunded me within minutes, no complaints at all here")
        self.assertEqual(kept, 0)
        self.assertEqual(out[0]["name"], "Airbnb", "lose the citation, not the incumbent")
        self.assertEqual(out[0]["quote"], "")
        self.assertIsNone(out[0]["rating"])

    def test_a_paraphrase_is_dropped(self):
        out, kept = _clean("Users say Airbnb overcharges and will not give the money back")
        self.assertEqual(kept, 0)
        self.assertEqual(out[0]["quote"], "")

    def test_a_fragment_too_short_to_prove_anything_is_dropped(self):
        """"is a scam" appears in half the corpus by chance; it proves no source."""
        short = "is a scam"
        self.assertLess(len(short), MIN_QUOTE_CHARS)
        self.assertIn(short, _norm(REVIEW), "the fragment IS in the body, and still fails")
        _out, kept = _clean(short)
        self.assertEqual(kept, 0)


if __name__ == "__main__":
    unittest.main()
