"""The scoring vocabularies must describe THIS corpus, and count PEOPLE, not words.

Measured over run pbfamily0001's 72,610 pain texts, the original lists were largely dead
letters: 8 of 12 REACHABILITY terms ("makerspace", "etsy", "seller", "hobbyist") fired fewer
than 50 times in the entire corpus, and 7 of 18 WTP terms ("invoice", "refund", "pricing")
were equally absent.

The rebuilt lists then matched over the theme's concatenated text, so one member's "premium"
lit the whole theme: 526 of 787 ranked themes sat at the WTP floor and WTP correlated +0.23
with rank, while intensity followed generic words ("hard", "frustrated", "can't"). Each text
component is now the SHARE of a theme's distinct authors carrying an evidence class, WTP is
graded by tense (money spent beats money imagined), and intensity by stated consequence.
"""
from __future__ import annotations

import unittest

from pipeline.s6_demand import (CONSEQUENCE_TERMS, INTENSITY_TERMS, REACHABILITY_TERMS,
                                WTP_STRONG_TERMS, WTP_WEAK_TERMS, _term_hits, score_cluster)

WEIGHTS = {"pain_intensity": 3, "frequency": 3, "willingness_to_pay": 3,
           "reachability": 2, "recurrence_score": 3}
SILENT = "the night market closes early on sundays"


def _theme(texts, authors=None, persona=""):
    """One pain per text. With no `authors`, author_hash is empty and the pain id stands in."""
    return {
        "id": "c1", "label": "t", "size": len(texts),
        "distinct_authors": len(set(authors)) if authors else len(texts),
        "pains": [{
            "id": "p%d" % i, "author_hash": authors[i] if authors else "",
            "complaint": "", "workflow_pain": "", "workaround": "", "wish": "",
            "persona": persona, "verbatim_span": text,
            "source_permalink": "https://example.test/%d" % i,
        } for i, text in enumerate(texts)],
    }


def _score(cluster):
    return score_cluster(cluster, WEIGHTS)


def _wtp(texts, **kw):
    return _score(_theme(texts, **kw))["willingness_to_pay"]


def _intensity(texts, **kw):
    return _score(_theme(texts, **kw))["pain_intensity"]


class WtpCountsAuthorsTests(unittest.TestCase):
    def test_one_member_saying_premium_does_not_light_the_theme(self):
        one_premium = _wtp(["premium is the only way to get seen"] + [SILENT] * 9)
        six_paying = _wtp(["I pay $30/mo for this app"] * 6 + [SILENT] * 4)
        self.assertLess(one_premium, six_paying)

    def test_one_loud_member_counts_once(self):
        loud = _wtp(["premium is a joke"] * 5 + [SILENT] * 9,
                    authors=["a"] * 5 + list("bcdefghij"))
        broad = _wtp(["premium is a joke"] * 5 + [SILENT] * 5,
                     authors=list("abcdefghij"))
        self.assertLess(loud, broad)

    def test_a_fraud_complaint_is_not_willingness_to_pay(self):
        score = _wtp(["this matchmaking agency is a scam"] * 5)
        self.assertEqual(score, 1.0)

    def test_money_already_spent_outranks_money_imagined(self):
        """The Mom Test: anything involving the future is an over-optimistic lie."""
        imagined = _wtp(["I would pay for an app that did this"] * 4)
        spent = _wtp(["I paid for an app that does this"] * 4)
        self.assertGreater(spent, imagined)

    def test_proof_of_payment_always_beats_a_mention_of_money(self):
        """The confusion that put "Expensive dates" at rank 1."""
        generic = _wtp(["flights and hotels cost a lot of money, very expensive"] * 5)
        real = _wtp(["I have been paying for premium for a year"] * 5)
        self.assertGreater(real, generic)

    def test_no_money_talk_scores_the_floor(self):
        result = _score(_theme(["the app keeps crashing on startup"] * 3))
        self.assertEqual(result["willingness_to_pay"], 1.0)
        self.assertEqual(result["scoring_evidence"]["willingness_to_pay"]["signals"], [])

    def test_the_evidence_shows_why_the_number_moved(self):
        texts = ["I pay $30/mo for this app"] * 6 + [SILENT] * 4
        ev = _score(_theme(texts))["scoring_evidence"]["willingness_to_pay"]
        strong = ev["wtp_strong"]
        self.assertEqual(strong["authors_with_signal"], 6)
        self.assertEqual(strong["distinct_authors"], 10, "an empty author_hash falls back to the pain id")
        self.assertEqual(strong["share"], 0.6)
        self.assertIn("$30", strong["signals"])
        self.assertIn("i pay", strong["signals"])
        self.assertEqual([q["quote"] for q in strong["quotes"]], [texts[0]] * 3)
        self.assertEqual(ev["wtp_weak"]["authors_with_signal"], 0)


class IntensityByConsequenceTests(unittest.TestCase):
    def test_a_stated_cost_outranks_an_emotion_word(self):
        cost = _intensity(["I missed my visa deadline and got fined"] * 5)
        mood = _intensity(["this is so hard and frustrating"] * 5)
        self.assertGreater(cost, mood)

    def test_the_extractors_persona_label_is_not_the_authors_words(self):
        """"frustrated user" gave 84 ranked pains a hit nobody wrote."""
        self.assertEqual(_intensity(["the bus schedule changed"] * 3, persona="frustrated user"), 3.0)

    def test_a_curly_apostrophe_is_still_an_apostrophe(self):
        self.assertGreater(_intensity(["I can’t get a visa appointment"] * 3), 3.0)


class VocabularyIsGroundedTests(unittest.TestCase):
    """Guard against the drift that made the old lists useless: terms nobody in a travel /
    dating forum would ever type."""

    DEAD_FOR_THIS_CORPUS = {"makerspace", "etsy", "hobbyist", "seller", "invoice",
                            "purchased", "pricing", "tedious", "manual"}

    def test_no_list_still_carries_a_dead_term(self):
        for name, terms in (("INTENSITY", INTENSITY_TERMS),
                            ("CONSEQUENCE", CONSEQUENCE_TERMS),
                            ("WTP_STRONG", WTP_STRONG_TERMS),
                            ("WTP_WEAK", WTP_WEAK_TERMS),
                            ("REACHABILITY", REACHABILITY_TERMS)):
            with self.subTest(list=name):
                self.assertEqual(terms & self.DEAD_FOR_THIS_CORPUS, set())

    def test_intensity_covers_how_people_here_actually_complain(self):
        text = "I am tired of this, it is a nightmare and I want to give up, the worst"
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
