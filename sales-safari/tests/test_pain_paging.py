"""Server-side pain paging, facet caps and word ranking.

These exist because run pbfamily0001 (74,849 pains, 8,925 themes) froze the browser tab:
the old endpoints shipped 41 MB and 22.8 MB of JSON before drawing a row.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.store import Store                      # noqa: E402
from pipeline.wordrank import rank_terms, terms_of    # noqa: E402


def _seed(store, run_id="r1"):
    c = store.conn
    c.execute("INSERT OR REPLACE INTO runs (job_id,seed_url,stage,status,created_at) "
              "VALUES (?,?,?,?,?)", (run_id, "seed", 7, "done", "2026-01-01T00:00:00Z"))
    rows = [
        # id, author, complaint, wish, workflow_pain, workaround, persona, canonical, posted
        ("p1", "a1", "visa paperwork is slow", "", "", "", "expat", "expat", "2026-01-04T00:00:00Z"),
        ("p2", "a2", "visa paperwork is slow", "", "", "", "expat", "expat", "2026-01-09T00:00:00Z"),
        ("p3", "a1", "", "wish the bank accepted 100% deposits", "", "", "nomad", "nomad", "2026-02-02T00:00:00Z"),
        ("p4", "a3", "", "", "renewing the permit each year", "", "nomad", "nomad", "2026-02-11T00:00:00Z"),
        ("p5", "a3", "", "", "", "pay an agent instead", "student", "student", "2026-03-01T00:00:00Z"),
    ]
    for pid, author, comp, wish, wf, wa, persona, canon, posted in rows:
        did = "d" + pid
        c.execute("INSERT INTO documents (id,run_id,source_type,source_url,permalink,"
                  "author_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                  (did, run_id, "forum", "http://x", "http://x/%s" % pid, author, posted))
        c.execute(
            "INSERT INTO pains (id,run_id,document_id,source_id,author_hash,complaint,"
            "workflow_pain,workaround,wish,persona,verbatim_span,source_permalink,"
            "created_at,persona_canonical,verified) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (pid, run_id, did, "src", author, comp, wf, wa, wish, persona,
             "quoted text", "http://x/%s" % pid, "2026-01-01T00:00:00Z", canon))
    # p1 and p2 sit in a theme; p3-p5 are unclustered.
    c.execute("INSERT INTO clusters (id,run_id,label,size,distinct_authors) "
              "VALUES (?,?,?,?,?)", ("c1", run_id, "Visa delays", 2, 2))
    for pid in ("p1", "p2"):
        c.execute("INSERT INTO cluster_members (cluster_id,pain_id) VALUES (?,?)", ("c1", pid))
    c.execute("INSERT INTO soft_filters (cluster_id,run_id,solvable,confidence,reason,warnings) "
              "VALUES (?,?,?,?,?,?)", ("c1", run_id, "yes", 0.9, "", "[]"))
    c.execute("INSERT INTO demand_scores (cluster_id,run_id,pain_intensity,frequency,"
              "willingness_to_pay,reachability,demand_score,evidence_count,distinct_authors) "
              "VALUES (?,?,?,?,?,?,?,?,?)", ("c1", run_id, 7.5, 2, 3, 4, 6.25, 2, 2))
    c.commit()


class PainPagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))
        _seed(self.store)

    def tearDown(self):
        self.store.close()

    def test_page_is_bounded_and_count_is_of_the_whole_match(self):
        page = self.store.get_pain_page("r1", limit=2)
        self.assertEqual(len(page), 2)
        self.assertEqual(self.store.count_pains_filtered("r1"), 5)

    def test_offset_walks_every_row_exactly_once(self):
        seen = []
        for off in (0, 2, 4):
            seen += [r["source"] for r in self.store.get_pain_page("r1", limit=2, offset=off)]
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_search_matches_any_text_column(self):
        self.assertEqual(self.store.count_pains_filtered("r1", q="paperwork"), 2)
        self.assertEqual(self.store.count_pains_filtered("r1", q="agent"), 1)
        self.assertEqual(self.store.count_pains_filtered("r1", q="Visa delays"), 2)  # theme label

    def test_percent_is_a_literal_not_a_wildcard(self):
        """A user searching "100%" used to match every row."""
        self.assertEqual(self.store.count_pains_filtered("r1", q="100%"), 1)
        self.assertEqual(self.store.count_pains_filtered("r1", q="_"), 0)

    def test_chips_filter_in_sql(self):
        self.assertEqual(self.store.count_pains_filtered("r1", types=["wish"]), 1)
        self.assertEqual(self.store.count_pains_filtered("r1", types=["wish", "workaround"]), 2)
        self.assertEqual(self.store.count_pains_filtered("r1", clustered="themed"), 2)
        self.assertEqual(self.store.count_pains_filtered("r1", clustered="unclustered"), 3)
        self.assertEqual(self.store.count_pains_filtered("r1", solvable=["yes"]), 2)
        self.assertEqual(self.store.count_pains_filtered("r1", solvable=["_none"]), 3)

    def test_unknown_type_name_is_ignored_not_injected(self):
        self.assertEqual(
            self.store.count_pains_filtered("r1", types=["complaint); DROP TABLE pains;--"]), 5)

    def test_persona_and_theme_filters(self):
        self.assertEqual(self.store.count_pains_filtered("r1", persona="nomad"), 2)
        self.assertEqual(self.store.count_pains_filtered("r1", theme="Visa delays"), 2)

    def test_facets_cap_the_pickers_and_report_the_real_totals(self):
        f = self.store.get_pain_facets("r1", limit=2)
        self.assertEqual(len(f["personas"]), 2)
        self.assertEqual(f["persona_total"], 3)
        self.assertEqual(f["theme_total"], 1)

    def test_cluster_details_can_be_asked_for_by_id(self):
        self.assertEqual(len(self.store.get_cluster_details("r1")), 1)
        self.assertEqual(self.store.get_cluster_details("r1", cluster_ids=["c1"])[0]["id"], "c1")
        self.assertEqual(self.store.get_cluster_details("r1", cluster_ids=[]), [])
        self.assertEqual(self.store.get_cluster_details("r1", cluster_ids=["nope"]), [])


class PainSortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))
        _seed(self.store)

    def tearDown(self):
        self.store.close()

    def _personas(self, **kw):
        return [r["persona_canonical"] for r in self.store.get_pain_page("r1", limit=99, **kw)]

    def test_sorts_both_ways(self):
        self.assertEqual(self._personas(sort="persona", direction="asc"),
                         ["expat", "expat", "nomad", "nomad", "student"])
        self.assertEqual(self._personas(sort="persona", direction="desc"),
                         ["student", "nomad", "nomad", "expat", "expat"])

    def test_blank_cells_sink_in_both_directions(self):
        """Three pains have no theme. SQLite would float those NULLs to the top of an
        ascending sort and fill page 1 with empty cells."""
        for way in ("asc", "desc"):
            themes = [r["theme"] for r in
                      self.store.get_pain_page("r1", limit=99, sort="theme", direction=way)]
            self.assertEqual(themes[:2], ["Visa delays", "Visa delays"], way)
            self.assertEqual(themes[2:], [None, None, None], way)

    def test_unknown_sort_key_is_ignored_not_injected(self):
        rows = self.store.get_pain_page("r1", limit=99, sort="p.id; DROP TABLE pains;--")
        self.assertEqual(len(rows), 5)

    def test_paging_a_sorted_column_never_repeats_a_row(self):
        seen = []
        for off in (0, 2, 4):
            seen += [r["source"] for r in
                     self.store.get_pain_page("r1", limit=2, offset=off, sort="theme")]
        self.assertEqual(len(set(seen)), 5)

    def test_runs_sort_whitelist(self):
        self.assertIn("pain_count", Store.RUN_SORTS)
        rows = self.store.list_runs(sort="nonsense; DROP TABLE runs;--")
        self.assertEqual([r["job_id"] for r in rows], ["r1"])


class VizDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.sqlite"))
        _seed(self.store)

    def tearDown(self):
        self.store.close()

    def test_theme_map_rows_carry_both_measures(self):
        out = self.store.get_viz_themes("r1")
        self.assertEqual(out["total"], 1)
        t = out["themes"][0]
        self.assertEqual((t["authors"], t["demand"], t["intensity"]), (2, 6.25, 7.5))
        self.assertEqual(t["solvable"], "yes")

    def test_theme_map_reports_what_the_cap_left_out(self):
        out = self.store.get_viz_themes("r1", limit=0)
        self.assertEqual(len(out["themes"]), 1)      # limit is floored at 1
        self.assertEqual(out["total"], 1)            # ...and total still counts them all

    def test_timeline_is_by_post_date_not_collection_date(self):
        tl = self.store.get_pain_timeline("r1")
        self.assertEqual(tl, [{"month": "2026-01", "pains": 2},
                              {"month": "2026-02", "pains": 2},
                              {"month": "2026-03", "pains": 1}])

    def test_personas_ranked_by_pains_with_author_counts(self):
        p = self.store.get_top_personas("r1")
        self.assertEqual(p[0]["persona"], "expat")
        self.assertEqual(p[0]["pains"], 2)
        self.assertEqual(p[0]["authors"], 2)
        self.assertEqual([x["persona"] for x in p], ["expat", "nomad", "student"])


class WordRankTests(unittest.TestCase):
    def test_phrases_do_not_jump_a_dropped_word(self):
        """"hard to get a visa" must not produce the phrase "hard visa"."""
        got = terms_of("hard to get a visa")
        self.assertIn("visa", got)
        self.assertNotIn("hard visa", got)

    def test_ranked_by_distinct_authors_not_repetition(self):
        rows = [("a1", "visa visa visa visa visa"),
                ("a1", "visa visa"),
                ("a2", "bank transfer"),
                ("a3", "bank transfer")]
        out = rank_terms(rows, min_pains=1, limit=10)
        by = {t["term"]: t for t in out["terms"]}
        self.assertEqual(by["visa"]["authors"], 1)
        self.assertEqual(by["bank"]["authors"], 2)
        self.assertLess(out["terms"].index(by["bank"]), out["terms"].index(by["visa"]))

    def test_rare_terms_are_dropped(self):
        rows = [("a1", "visa delay"), ("a2", "visa delay"), ("a3", "unicorn")]
        terms = {t["term"] for t in rank_terms(rows, min_pains=2)["terms"]}
        self.assertIn("visa", terms)
        self.assertNotIn("unicorn", terms)

    def test_empty_corpus_is_not_an_error(self):
        self.assertEqual(rank_terms([], min_pains=1)["terms"], [])


if __name__ == "__main__":
    unittest.main()
