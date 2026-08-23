"""The scoring vocabularies must describe THIS corpus, not a maker/B2B one.

Measured over run pbfamily0001's 72,610 pain texts, the original lists were largely dead
letters: 8 of 12 REACHABILITY terms ("makerspace", "etsy", "seller", "hobbyist") fired fewer
than 50 times in the entire corpus, and 7 of 18 WTP terms ("invoice", "refund", "pricing")
were equally absent. `willingness_to_pay` never moved off its base on 90% of themes and
`pain_intensity` never moved on 68%.

Worse than dead, WTP measured the wrong thing. A flat keyword bag scores "expensive drinks"
and "expensive premium subscription" identically, which is how "Expensive dates" reached
rank 1 and "Overpriced items" rank 6. WTP is now tiered: proof that money changed hands for
a product or service outweighs any mention of money.
"""
from __future__ import annotations

import unittest

from pipeline.s6_demand import (INTENSITY_TERMS, REACHABILITY_TERMS, WTP_STRONG_TERMS,
                                WTP_WEAK_TERMS, _term_hits, _wtp_score)


class TieredWtpTests(unittest.TestCase):
    def test_generic_spending_scores_low(self):
        """The exact confusion that put "Expensive dates" at rank 1."""
        self.assertLess(_wtp_score("drinks here are expensive")[0], 2.5)

    def test_paying_for_a_product_scores_high(self):
        self.assertGreater(_wtp_score("I am paying for premium and it is not worth it")[0], 7.5)

    def test_a_verdict_on_a_paid_product_scores_high(self):
        self.assertGreater(_wtp_score("the subscription is overpriced, a total scam")[0], 7.5)

    def test_proof_of_payment_always_beats_a_mention_of_money(self):
        generic = _wtp_score("flights and hotels cost a lot of money, very expensive")[0]
        real = _wtp_score("I have been paying for premium for a year")[0]
        self.assertGreater(real, generic)

    def test_no_money_talk_scores_the_floor(self):
        score, hits = _wtp_score("the app keeps crashing on startup")
        self.assertEqual(score, 1.0)
        self.assertEqual(hits, [])

    def test_the_tiers_do_not_overlap_on_multi_word_intent(self):
        """"pay for" is strong; the bare verb "pay" is weak. Both may fire; that is fine."""
        self.assertIn("pay for", WTP_STRONG_TERMS)
        self.assertIn("pay", WTP_WEAK_TERMS)
        self.assertNotIn("pay for", WTP_WEAK_TERMS)


class VocabularyIsGroundedTests(unittest.TestCase):
    """Guard against the drift that made the old lists useless: terms nobody in a travel /
    dating forum would ever type."""

    DEAD_FOR_THIS_CORPUS = {"makerspace", "etsy", "hobbyist", "seller", "invoice",
                            "purchased", "pricing", "tedious", "manual"}

    def test_no_list_still_carries_a_dead_term(self):
        for name, terms in (("INTENSITY", INTENSITY_TERMS),
                            ("WTP_STRONG", WTP_STRONG_TERMS),
                            ("WTP_WEAK", WTP_WEAK_TERMS),
                            ("REACHABILITY", REACHABILITY_TERMS)):
            with self.subTest(list=name):
                self.assertEqual(terms & self.DEAD_FOR_THIS_CORPUS, set())

    def test_intensity_covers_how_people_here_actually_complain(self):
        text = "I am tired of this, it is a nightmare and I gave up, the worst"
        hits = _term_hits(text, INTENSITY_TERMS)
        self.assertGreaterEqual(len(hits), 4, hits)

    def test_reachability_names_where_these_buyers_gather(self):
        text = "the expats in that discord group and the subreddit all use tinder"
        hits = _term_hits(text, REACHABILITY_TERMS)
        self.assertGreaterEqual(len(hits), 4, hits)

    def test_multi_word_terms_match_across_whitespace(self):
        """_term_hits collapses runs of whitespace; a term with a space must still fire."""
        self.assertIn("tired of", _term_hits("I am  tired   of it", INTENSITY_TERMS))


if __name__ == "__main__":
    unittest.main()
