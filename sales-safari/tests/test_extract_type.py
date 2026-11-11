import json
import sqlite3
import sys
from pathlib import Path

import pytest

from pipeline import extract
from pipeline.collectors.base import Document
from pipeline.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compare_extract_type  # noqa: E402

RUN = "r1"


def _store_with_posts(tmp_path, posts):
    """`posts` = {title: body}. Returns the store; titles route the fake extractor."""
    store = Store(str(tmp_path / "s.sqlite"))
    store.start_run(RUN, "https://ex.com/")
    for title, body in posts.items():
        url = f"https://ex.com/{title}"
        store.upsert_document(RUN, Document(
            source_type="forum", source_url=url, permalink=url, title=title,
            raw_markdown=body, author=f"author-{title}", thread_url=url + "/t"))
    return store


def _fake_extractor(monkeypatch, items_by_title):
    """Answer each batch with the items listed for its posts' titles."""
    def fake(prompt, extract_cfg=None):
        posts = json.loads(prompt[len(extract.PROMPT_HEADER):])
        return json.dumps([{**item, "post_id": p["id"]}
                           for p in posts for item in items_by_title.get(p["title"], [])]), "fake"
    monkeypatch.setattr(extract, "_call_extractor", fake)


def _pains(store):
    rows = store.conn.execute(
        "SELECT d.title, p.extract_type, p.pain_type, p.verified, p.verify_reason "
        "FROM pains p JOIN documents d ON d.id=p.document_id").fetchall()
    return {r[0]: r[1:] for r in rows}


def test_normalize_pain_type_accepts_only_the_vocabulary():
    assert extract.normalize_pain_type("  Product_Friction\n") == "product_friction"
    assert extract.normalize_pain_type("WISH") == "wish"
    assert extract.normalize_pain_type("complaint") == ""
    assert extract.normalize_pain_type("unjudged") == ""
    assert extract.normalize_pain_type("") == ""
    assert extract.normalize_pain_type(None) == ""


@pytest.mark.parametrize("prompt", ["PROMPT_HEADER", "VERIFY_PROMPT_HEADER"])
def test_every_pain_type_is_offered_as_a_choice_in_each_prompt(prompt):
    text = getattr(extract, prompt)
    missing = [t for t in extract.PAIN_TYPES if f"\n- {t}:" not in text]
    assert missing == []


def test_extract_prompt_asks_for_a_type_without_letting_it_gate_recall():
    schema = next(line for line in extract.PROMPT_HEADER.splitlines()
                  if line.startswith('{"post_id"'))
    assert '"pain_type"' in schema
    assert "NEVER decides whether" in extract.PROMPT_HEADER
    assert "Do NOT self-censor advice, opinion, or debate" in extract.PROMPT_HEADER


def test_extract_run_stores_a_valid_tag_and_null_otherwise(tmp_path, monkeypatch):
    store = _store_with_posts(tmp_path, {
        "valid": "Exporting the report by hand every Monday wastes my whole morning.",
        "invalid": "The invoice tool keeps crashing whenever I attach a receipt photo.",
        "missing": "I wish my scheduling app could sync with the shared family calendar.",
    })
    _fake_extractor(monkeypatch, {
        "valid": [{"complaint": "manual export", "pain_type": "  Product_Friction ",
                   "verbatim_span": "Exporting the report by hand every Monday wastes my whole morning."}],
        "invalid": [{"complaint": "tool crashes", "pain_type": "complaint",
                     "verbatim_span": "The invoice tool keeps crashing whenever I attach a receipt photo."}],
        "missing": [{"wish": "calendar sync",
                     "verbatim_span": "I wish my scheduling app could sync with the shared family calendar."}],
    })

    stats = extract.extract_run(store, RUN)

    assert _pains(store) == {
        "valid": ("product_friction", None, None, None),
        "invalid": (None, None, None, None),
        "missing": (None, None, None, None),
    }
    assert (stats["kept"], stats["typed"], stats["untyped"]) == (3, 1, 2)
    assert stats["by_extract_type"] == {"product_friction": 1}


def test_span_gate_drops_an_invented_quote_whatever_its_tag(tmp_path, monkeypatch):
    store = _store_with_posts(tmp_path, {
        "post": "The checkout page times out every time I try to pay with a card.",
    })
    _fake_extractor(monkeypatch, {"post": [{
        "complaint": "checkout fails", "pain_type": "product_friction",
        "verbatim_span": "The checkout page is completely broken for everyone.",
    }]})

    stats = extract.extract_run(store, RUN)

    assert _pains(store) == {}
    assert (stats["kept"], stats["dropped"], stats["typed"]) == (0, 1, 0)


def test_non_keep_tags_are_still_inserted(tmp_path, monkeypatch):
    store = _store_with_posts(tmp_path, {
        "advice": "You should always keep a second phone for banking apps, honestly.",
        "joke": "Friday afternoon and I'm already three martinis down, send help.",
    })
    _fake_extractor(monkeypatch, {
        "advice": [{"complaint": "phone advice", "pain_type": "advice_opinion",
                    "verbatim_span": "You should always keep a second phone for banking apps"}],
        "joke": [{"complaint": "martini joke", "pain_type": "off_topic",
                  "verbatim_span": "Friday afternoon and I'm already three martinis down"}],
    })

    stats = extract.extract_run(store, RUN)

    assert _pains(store) == {
        "advice": ("advice_opinion", None, None, None),
        "joke": ("off_topic", None, None, None),
    }
    assert stats["by_extract_type"] == {"advice_opinion": 1, "off_topic": 1}


def test_insert_pain_without_extract_type_stores_null(tmp_path):
    store = _store_with_posts(tmp_path, {})
    assert store.insert_pain(RUN, {"document_id": "d1", "verbatim_span": "an older caller's pain"})
    assert store.conn.execute("SELECT extract_type FROM pains").fetchall() == [(None,)]


def test_migration_adds_the_column_to_an_existing_database(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pains(id TEXT PRIMARY KEY, run_id TEXT, document_id TEXT, source_id TEXT,"
        " author_hash TEXT, complaint TEXT, workflow_pain TEXT, workaround TEXT, wish TEXT,"
        " persona TEXT, verbatim_span TEXT, span_start INTEGER, span_end INTEGER,"
        " source_permalink TEXT, verified INTEGER, pain_type TEXT, verify_reason TEXT,"
        " created_at TEXT)")
    conn.execute("INSERT INTO pains(id, run_id, document_id, verbatim_span) "
                 "VALUES('old', 'r0', 'd0', 'a pain stored before the column existed')")
    conn.commit()
    conn.close()

    reader = Store(str(path), readonly=True)
    assert reader.extract_type_vs_verdict("r0") == []
    reader.close()
    assert "extract_type" not in {r[1] for r in sqlite3.connect(path).execute(
        "PRAGMA table_info(pains)")}

    store = Store(str(path))
    store.insert_pain("r0", {"document_id": "d1", "verbatim_span": "a newer tagged pain here",
                             "extract_type": "wish"})
    assert sorted(store.conn.execute(
        "SELECT document_id, extract_type FROM pains").fetchall()) == [("d0", None), ("d1", "wish")]


SEEDED = [  # (extract_type, pain_type)
    ("product_friction", "product_friction"),
    ("product_friction", "product_friction"),
    ("wish", "wish"),
    ("wish", "social_complaint"),
    ("advice_opinion", "wish"),
    ("off_topic", "cost_complaint"),
    ("off_topic", "off_topic"),
    ("advice_opinion", "advice_opinion"),
    ("cost_complaint", None),
    ("advice_opinion", None),
    ("off_topic", "unjudged"),
    (None, "product_friction"),
]


def _run_script(tmp_path, capsys, db):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("verify:\n  keep_types: [product_friction, wish, cost_complaint]\n")
    assert compare_extract_type.main([RUN, "--db", str(db), "--config", str(cfg)]) == 0
    return capsys.readouterr().out.splitlines()


def test_compare_script_prints_matrix_and_keep_precision_recall(tmp_path, capsys):
    store = _store_with_posts(tmp_path, {})
    for i, (etype, ptype) in enumerate(SEEDED):
        store.insert_pain(RUN, {"document_id": f"d{i}", "verbatim_span": f"seeded pain number {i}",
                                "extract_type": etype})
        if ptype:
            pid = store.conn.execute("SELECT id FROM pains WHERE document_id=?",
                                     (f"d{i}",)).fetchone()[0]
            store.set_pain_verdict(pid, ptype, 1, "seed")
    store.close()

    out = _run_script(tmp_path, capsys, tmp_path / "s.sqlite")
    rows = {line.split()[0]: [int(x) for x in line.split()[1:]]
            for line in out if line.split() and line.split()[0] in (*extract.PAIN_TYPES, "total")}
    # columns: product_friction wish cost_complaint social_complaint advice_opinion off_topic total
    assert rows["product_friction"] == [2, 0, 0, 0, 0, 0, 2]
    assert rows["wish"] == [0, 1, 0, 1, 0, 0, 2]
    assert rows["advice_opinion"] == [0, 1, 0, 0, 1, 0, 2]
    assert rows["off_topic"] == [0, 0, 1, 0, 0, 1, 2]
    assert rows["total"] == [2, 2, 1, 1, 1, 1, 8]

    def stat(name):
        return next(line.split() for line in out if line.strip().startswith(name))
    assert stat("precision")[1:5] == ["0.750", "3", "of", "4"]
    assert stat("recall")[1:5] == ["0.600", "3", "of", "5"]
    assert stat("non-keep")[1:4] == ["6", "of", "11"]
    assert "(54.5%)" in stat("non-keep")
    assert any("1 tagged pains whose 3b pain_type is not one of the six" in line for line in out)


def test_compare_script_says_so_when_nothing_overlaps(tmp_path, capsys):
    store = _store_with_posts(tmp_path, {})
    store.insert_pain(RUN, {"document_id": "d0", "verbatim_span": "tagged but never judged",
                            "extract_type": "wish"})
    store.close()

    out = _run_script(tmp_path, capsys, tmp_path / "s.sqlite")

    assert len(out) == 1
    assert out[0].startswith("No overlap") and "(1 carry a 3a tag)" in out[0]
