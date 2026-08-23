"""Rank the words and two-word phrases that the most DIFFERENT people complain about.

Why distinct authors and not raw frequency: one prolific poster who writes "visa" forty
times in one thread is one person's problem. The rest of the pipeline already ranks themes
by `distinct_authors` for that reason (s9_rank), so the word list uses the same yardstick
and the two views agree.

Two passes over the pains, not one: holding an author set for every candidate term needs
hundreds of MB on a 75k-pain run. Pass 1 counts cheaply and throws away the long tail;
pass 2 only tracks authors for the terms that survived.
"""
from collections import Counter, defaultdict
import re

# Deliberately small. A domain stop list ("visa", "flight") would hide the answer -- these
# are only words that carry no topic anywhere: function words, and the filler that the
# extractor itself puts in every row ("user", "wants", "needs").
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "they", "them", "their", "there", "then",
    "than", "have", "has", "had", "was", "were", "are", "but", "not", "you", "your",
    "his", "her", "she", "him", "its", "our", "out", "who", "whom", "what", "when",
    "where", "which", "while", "will", "would", "could", "should", "can", "cant",
    "does", "did", "doing", "done", "get", "gets", "got", "getting", "make", "makes",
    "made", "making", "want", "wants", "wanted", "wanting", "need", "needs", "needed",
    "like", "likes", "liked", "just", "one", "two", "some", "any", "all", "also",
    "more", "most", "much", "many", "very", "too", "own", "same", "such", "into",
    "from", "about", "after", "before", "over", "under", "again", "once", "here",
    "how", "why", "because", "been", "being", "were", "wasnt", "isnt", "dont", "doesnt",
    "user", "users", "people", "someone", "anyone", "everyone", "thing", "things",
    "way", "ways", "time", "times", "lot", "bit", "etc", "know", "knows", "think",
    "thinks", "say", "says", "said", "see", "sees", "look", "looking", "find", "finds",
    "finding", "use", "uses", "used", "using", "take", "takes", "taken", "taking",
    "give", "gives", "given", "come", "comes", "went", "goes", "going", "still", "even",
    "never", "always", "every", "other", "another", "without", "cannot", "unable",
    "only", "back", "down", "off", "yes", "actually", "really", "probably", "maybe",
    "though", "although", "since", "until", "both", "each", "few", "far", "yet",
    "these", "those", "now", "well", "lots", "kind", "sort", "due", "able", "sure",
    "having", "getting", "looking", "trying", "saying", "seeing", "telling",
    # Contractions survive tokenisation because the apostrophe is kept inside a word;
    # "don't" ranked 5th on the first pass, above every real topic word.
    "dont", "don't", "doesn't", "didn't", "can't", "won't", "isn't", "aren't",
    "wasn't", "weren't", "couldn't", "wouldn't", "shouldn't", "haven't", "hasn't",
    "it's", "i'm", "you're", "they're", "we're", "that's", "there's", "he's", "she's",
    "i've", "you've", "we've", "they've", "i'll", "you'll", "we'll", "they'll",
}

_WORD = re.compile(r"[a-z][a-z0-9'+/-]*")
MIN_TERM_LEN = 3


def tokenize(text: str):
    """Content words in order, with a None wherever a stopword or number was dropped.

    The gap marker matters: without it "hard to get a visa" would yield the phrase
    "hard visa", a phrase nobody wrote.
    """
    out = []
    for m in _WORD.finditer((text or "").lower()):
        w = m.group(0).strip("'-+/")
        if len(w) < MIN_TERM_LEN or w in STOPWORDS or w.isdigit():
            out.append(None)
        else:
            out.append(w)
    return out


def terms_of(text: str):
    """Distinct unigrams and adjacent-pair bigrams in one pain."""
    toks = tokenize(text)
    found = set()
    for i, w in enumerate(toks):
        if w is None:
            continue
        found.add(w)
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt is not None and nxt != w:
            found.add(w + " " + nxt)
    return found


def rank_terms(rows, min_pains: int = 15, limit: int = 200):
    """rows: iterable of (author_hash, text), re-iterable (called twice).

    Returns terms sorted by distinct authors, then by pains, then alphabetically so the
    order is stable between two identical runs.
    """
    counts = Counter()
    total_pains = 0
    for _author, text in rows:
        total_pains += 1
        counts.update(terms_of(text))

    kept = {t for t, n in counts.items() if n >= min_pains}
    if not kept:
        return {"total_pains": total_pains, "terms": []}

    authors = defaultdict(set)
    for author, text in rows:
        if not author:
            continue
        for t in terms_of(text):
            if t in kept:
                authors[t].add(author)

    ranked = sorted(
        ({"term": t, "pains": counts[t], "authors": len(authors.get(t, ())),
          "words": 2 if " " in t else 1} for t in kept),
        key=lambda d: (-d["authors"], -d["pains"], d["term"]))
    return {"total_pains": total_pains, "terms": ranked[:max(1, limit)]}


class ReiterableRows:
    """rank_terms walks the rows twice; a DB cursor only walks once."""

    def __init__(self, factory):
        self._factory = factory

    def __iter__(self):
        return iter(self._factory())
