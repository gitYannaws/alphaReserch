"""Stage 10: generate concrete product idea stubs for top-ranked themes."""

import re
from collections import Counter


_STOPWORDS = {
    "about", "after", "again", "against", "because", "being", "between", "could",
    "every", "foreign", "having", "people", "really", "should", "their", "there",
    "these", "thing", "things", "those", "through", "travel", "where", "which",
    "while", "women", "would", "youre", "your", "they", "them", "with", "from",
    "that", "this", "into", "than", "then", "when", "what", "were", "will",
    "also", "and", "any", "are", "bad", "been", "best", "but", "can", "cant",
    "did", "does", "dont", "for", "get", "got", "great", "had", "has", "have",
    "her", "him", "his", "how", "its", "like", "look", "looks", "many", "more",
    "most", "not", "off", "one", "our", "out", "same", "she", "some", "the",
    "back", "example", "good", "immediately", "other", "very", "was", "who",
    "why", "you",
}

_CITY_WORDS = {
    "bangkok", "pattaya", "phuket", "chiang", "manila", "cebu", "medellin",
    "medellín", "bogota", "bogotá", "cartagena", "bali", "jakarta", "tokyo",
    "osaka", "hanoi", "saigon", "mexico", "cancun", "lima", "rio", "madrid",
    "cape", "verde", "valencia", "prague", "istanbul", "dubai",
}

_CATEGORY_RULES = [
    (
        "destination",
        ("island", "city", "country", "beach", "nightlife", "locals", "travelers",
         "nature", "where to go", "best place", "cape verde", "dating abroad"),
        "TripFit Brief",
        "a destination shortlisting assistant",
        "turns scattered forum advice into ranked city/country options by nightlife, safety, budget, dating pool, language friction, and trip style",
    ),
    (
        "safety",
        ("stole", "theft", "scam", "kidnap", "abduct", "danger", "unsafe", "police",
         "cash", "robbed", "guesthouse", "tinder scam"),
        "Streetwise Date Ledger",
        "a safety and scam-pattern tracker",
        "captures real incident reports, flags repeat risk patterns by city/venue/app, and gives travelers a pre-date checklist",
    ),
    (
        "app_quality",
        ("app is garbage", "vibe coded", "bug", "broken app", "fake profiles",
         "low match", "dating app", "ghost", "disappear"),
        "Match Market Audit",
        "a dating-app quality monitor",
        "compares app reliability, fake-profile density, response quality, and recent user complaints by destination",
    ),
    (
        "relationship_expectations",
        ("traditional", "submissive", "provide", "treated like", "wife", "marriage",
         "race", "power", "income gap", "expectations"),
        "Expectation Compass",
        "a cross-cultural expectation checker",
        "helps travelers compare stated dating expectations, power dynamics, commitment norms, and financial assumptions before investing in a place or person",
    ),
    (
        "fitness_lifestyle",
        ("fit", "fitness", "lifestyle", "maintain", "time and effort", "gym"),
        "Lifestyle Match Filter",
        "a lifestyle-compatibility screener",
        "filters destinations and dating channels by health habits, daily routine fit, social pace, and maintenance effort",
    ),
]


def _clean_label(label: str) -> str:
    label = " ".join((label or "market pain").split())
    return label[:95].rstrip(" .")


def _all_text(cluster: dict) -> str:
    parts = [cluster.get("label") or ""]
    for pain in cluster.get("pains", []):
        parts.extend([
            pain.get("complaint") or "",
            pain.get("workflow_pain") or "",
            pain.get("workaround") or "",
            pain.get("wish") or "",
            pain.get("verbatim_span") or "",
        ])
    return " ".join(parts)


def _choose_category(text: str):
    low = text.lower()
    for key, triggers, title, noun, promise in _CATEGORY_RULES:
        if any(t in low for t in triggers):
            return key, title, noun, promise
    if any(city in low for city in _CITY_WORDS):
        key, _triggers, title, noun, promise = _CATEGORY_RULES[0]
        return key, title, noun, promise
    return (
        "research",
        "Signal Board",
        "a community evidence board",
        "turns recurring forum complaints into cited decision cards, tradeoffs, and next-step questions",
    )


def _keywords(text: str, limit: int = 4):
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text.lower())
    counts = Counter(w.strip("'") for w in words if w not in _STOPWORDS)
    return [w for w, _ in counts.most_common(limit)]


def _title_suffix(category: str, text: str, keywords: list[str]) -> str:
    low = text.lower()
    phrase_rules = {
        "destination": [
            (("island", "nightlife"), "Island Nightlife"),
            (("beach", "nature"), "Beach Nature Fit"),
            (("city", "country"), "City Shortlist"),
            (("locals", "travelers"), "Local Social Fit"),
        ],
        "safety": [
            (("stole", "cash"), "Cash Theft"),
            (("guesthouse", "room"), "Guesthouse Risk"),
            (("scam", "tinder"), "Tinder Scams"),
            (("robbed", "police"), "Robbery Response"),
        ],
        "app_quality": [
            (("app", "garbage"), "App Quality"),
            (("vibe", "coded"), "Bug Reports"),
            (("fake", "profiles"), "Fake Profiles"),
            (("match", "ghost"), "Match Quality"),
        ],
        "relationship_expectations": [
            (("submissive", "race"), "Dating Expectations"),
            (("income", "provide"), "Money Expectations"),
            (("wife", "marriage"), "Marriage Intent"),
            (("power", "expectations"), "Power Dynamics"),
        ],
        "fitness_lifestyle": [
            (("fit", "lifestyle"), "Fitness Fit"),
            (("gym", "routine"), "Gym Routine"),
            (("time", "effort"), "Maintenance Effort"),
        ],
    }
    for needles, suffix in phrase_rules.get(category, []):
        if all(needle in low for needle in needles):
            return suffix
    return " / ".join(k.title() for k in keywords[:2])


def _persona(cluster: dict) -> str:
    personas = [
        (p.get("persona_canonical") or p.get("persona") or "").strip()
        for p in cluster.get("pains", [])
    ]
    personas = [p for p in personas if p]
    if not personas:
        return "travelers researching dating abroad"
    return Counter(personas).most_common(1)[0][0]


def _evidence_line(pain: dict, fallback: str) -> str:
    text = (
        pain.get("workflow_pain")
        or pain.get("complaint")
        or pain.get("wish")
        or pain.get("verbatim_span")
        or fallback
    )
    return " ".join(text.split())[:180].rstrip()


def _idea_for(cluster: dict) -> tuple[str, str, str]:
    text = _all_text(cluster)
    category, base_title, noun, promise = _choose_category(text)
    kws = _keywords(text)
    persona = _persona(cluster)
    evidence = (cluster.get("pains") or [{}])[0]
    label = _clean_label(cluster.get("label"))
    evidence_line = _evidence_line(evidence, label)
    suffix = _title_suffix(category, text, kws)
    title = f"{base_title}: {suffix}" if suffix else base_title
    pitch = (
        f"For {persona}, {noun} that {promise}. "
        f"It starts from cited community evidence like: \"{evidence_line}\""
    )
    return title[:110], pitch, evidence.get("source_permalink") or ""


def ideas_run(store, run_id: str, top_n: int = 5, progress=None) -> dict:
    ranked = store.get_ranked_clusters(run_id)[:top_n]
    details = {c["id"]: c for c in store.get_cluster_details(run_id)}
    store.clear_ideas(run_id)
    made = 0
    if progress:
        progress(0, len(ranked))
    for i, row in enumerate(ranked, start=1):
        cluster = details.get(row["cluster_id"])
        if not cluster:
            if progress:
                progress(i, len(ranked))
            continue
        title, pitch, permalink = _idea_for(cluster)
        store.save_idea(run_id, cluster["id"], title, pitch, permalink)
        made += 1
        if progress:
            progress(i, len(ranked))
    store.set_stage(run_id, 10, "ideas_generated")
    return {"ideas": made}
