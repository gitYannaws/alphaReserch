"""Owner yay/nay votes and idea regenerate.

The votes are the only human ground truth in the project, and cluster ids are reused when a
run is re-clustered, so a vote must carry its own snapshot and still find its theme after.
Every test runs on a temp SQLite file: the web app's module-level DB path is pointed at it,
and the tests read their rows back from that file.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import webapp.app as web                      # noqa: E402
from bench import score_ranking               # noqa: E402
from pipeline.store import Store              # noqa: E402

RUN = "r1"
LIVE_DB = (ROOT / "db" / "safari.sqlite").resolve()
GOLD_KEYS = {"kind", "verdict", "label", "title", "member_pain_ids", "rank_at_vote", "note",
             "created_at"}


def _pains(store, ids):
    for pid in ids:
        store.conn.execute(
            "INSERT INTO pains(id,run_id,document_id,complaint,verbatim_span,source_permalink) "
            "VALUES(?,?,?,?,?,?)", (pid, RUN, "d", f"complaint {pid}", f"span {pid}",
                                    f"https://forum.test/{pid}"))
    store.conn.commit()


def _rank(store, cluster_id, rank, dropped=False):
    store.save_ranking(RUN, {"cluster_id": cluster_id, "rank": rank, "rank_score": 1.0,
                             "demand_score": 1.0, "persistence_score": 1.0,
                             "saturation_score": 0.0, "dropped": dropped,
                             "filter_reasons": []})


def _recluster(store):
    """What a stage-5 re-run does: same `<run>-<n>` ids, different themes behind them."""
    store.clear_clusters(RUN)
    store.clear_rankings(RUN)
    store.save_cluster("r1-0", RUN, "Scheduling is a mess", 4, 4, ["p5", "p6", "p7", "p8"])
    store.save_cluster("r1-1", RUN, "Lost invoices", 4, 4, ["p1", "p2", "p3", "p9"])
    _rank(store, "r1-1", 1)
    _rank(store, "r1-0", 2)


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "safari.sqlite")
    store = Store(db)
    store.start_run(RUN, "https://forum.test/board")
    store.set_run_status(RUN, 12, "done")
    _pains(store, [f"p{i}" for i in range(1, 10)])
    store.save_cluster("r1-0", RUN, "Invoices get lost", 4, 4, ["p1", "p2", "p3", "p4"])
    store.save_cluster("r1-1", RUN, "Scheduling chaos", 3, 3, ["p5", "p6", "p7"])
    _rank(store, "r1-0", 1)
    _rank(store, "r1-1", 2)
    idea_id = store.save_idea(RUN, "r1-1", "Old Title", "Old pitch.", "https://forum.test/p5")
    store.save_brief(RUN, idea_id, "r1-1", {"problem": "old problem", "wedge": "old wedge"})
    store.close()
    monkeypatch.setattr(web, "DB", db)
    assert web.DB == db and Path(web.DB).resolve() != LIVE_DB
    return SimpleNamespace(db=db, client=TestClient(web.app), idea_id=idea_id)


def _vote(env, **body):
    return env.client.post(f"/api/run/{RUN}/feedback", json=body)


def _stored(env, fn, *args):
    store = Store(env.db)
    try:
        return getattr(store, fn)(*args)
    finally:
        store.close()


def test_vote_then_change_then_clear(env):
    assert _vote(env, kind="theme", cluster_id="r1-0", verdict="yay").status_code == 200
    rows = _stored(env, "get_feedback", RUN)          # read from the temp file itself
    assert [(v["cluster_id"], v["verdict"]) for v in rows] == [("r1-0", "yay")]

    r = _vote(env, kind="theme", cluster_id="r1-0", verdict="nay", note="  too\nbroad ")
    assert r.json()["vote"]["verdict"] == "nay"
    votes = env.client.get(f"/api/run/{RUN}/feedback").json()
    assert [(v["verdict"], v["note"], v["current"]) for v in votes] == [("nay", "too broad", True)]

    assert _vote(env, kind="theme", cluster_id="r1-0", verdict="clear").json() == {"vote": None}
    assert env.client.get(f"/api/run/{RUN}/feedback").json() == []
    assert _stored(env, "get_feedback", RUN) == []


def test_snapshot_comes_from_the_db_not_the_client(env):
    r = _vote(env, kind="theme", cluster_id="r1-0", verdict="yay",
              label="LIE", member_pain_ids=["x"], rank_at_vote=99, title="LIE")
    assert r.status_code == 200
    [v] = _stored(env, "get_feedback", RUN)
    assert (v["label"], v["member_pain_ids"], v["rank_at_vote"], v["title"]) == \
        ("Invoices get lost", ["p1", "p2", "p3", "p4"], 1, None)


def test_idea_vote_snapshots_its_title_and_theme(env):
    r = _vote(env, kind="idea", cluster_id="r1-0", idea_id=env.idea_id, verdict="nay")
    assert r.status_code == 200
    [v] = _stored(env, "get_feedback", RUN)
    # The idea's own theme, not the cluster_id the client sent.
    assert (v["title"], v["cluster_id"], v["label"], v["member_pain_ids"], v["rank_at_vote"]) == \
        ("Old Title", "r1-1", "Scheduling chaos", ["p5", "p6", "p7"], 2)


def test_vote_survives_recluster_and_maps_to_its_successor(env):
    assert _vote(env, kind="theme", cluster_id="r1-0", verdict="yay").status_code == 200
    store = Store(env.db)
    _recluster(store)
    store.close()

    [v] = env.client.get(f"/api/run/{RUN}/feedback").json()
    assert (v["label"], v["member_pain_ids"], v["current"]) == \
        ("Invoices get lost", ["p1", "p2", "p3", "p4"], False)

    gold = env.client.get(f"/api/run/{RUN}/feedback/export").json()
    ro = Store(env.db, readonly=True)
    try:
        mapped, unmapped = score_ranking.map_votes(gold["votes"], ro.get_theme_members(RUN))
        out = score_ranking.score(gold, ro, RUN)
    finally:
        ro.close()
    # {p1..p4} vs the new r1-1 {p1,p2,p3,p9}: 3 shared / 5 in the union = 0.6.
    assert {cid: j for cid, (_v, j) in mapped.items()} == {"r1-1": pytest.approx(0.6)}
    assert unmapped == []
    assert out["precision"][10] == (1.0, 1)


def test_vote_on_a_reused_id_keeps_the_vote_on_the_old_theme(env):
    assert _vote(env, kind="theme", cluster_id="r1-0", verdict="yay").status_code == 200
    store = Store(env.db)
    _recluster(store)
    store.close()
    assert _vote(env, kind="theme", cluster_id="r1-0", verdict="nay").status_code == 200
    votes = env.client.get(f"/api/run/{RUN}/feedback").json()
    assert [(v["label"], v["verdict"], v["current"]) for v in votes] == [
        ("Invoices get lost", "yay", False), ("Scheduling is a mess", "nay", True)]


@pytest.mark.parametrize("body, status, error", [
    ({"kind": "theme", "cluster_id": "r1-0", "verdict": "maybe"}, 400, "verdict must be"),
    ({"kind": "pain", "cluster_id": "r1-0", "verdict": "yay"}, 400, "kind must be"),
    ({"kind": "theme", "verdict": "yay"}, 400, "cluster_id required"),
    ({"kind": "idea", "cluster_id": "r1-1", "verdict": "yay"}, 400, "idea_id required"),
    ({"kind": "theme", "cluster_id": "r1-404", "verdict": "yay"}, 404, "unknown theme"),
    ({"kind": "idea", "idea_id": "nope", "verdict": "nay"}, 404, "unknown idea"),
    ({"kind": "theme", "cluster_id": "r1-0"}, 422, None),
])
def test_invalid_votes_are_rejected(env, body, status, error):
    r = _vote(env, **body)
    assert r.status_code == status
    if error:
        assert error in r.json()["error"]
    assert _stored(env, "get_feedback", RUN) == []


def _model(reply):
    calls = []

    def fake(prompt, cfg):
        calls.append((prompt, cfg))
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply), "claude"
    return fake, calls


def test_regenerate_replaces_the_idea_keeps_its_id_and_drops_its_brief(env, monkeypatch):
    monkeypatch.setitem(web.CFG, "ideas", {**web.CFG.get("ideas", {}), "model": "idea-model"})
    _vote(env, kind="idea", cluster_id="r1-1", idea_id=env.idea_id, verdict="nay")
    fake, calls = _model([{"id": "r1-1", "title": "Shift Swap Board", "pitch": "New pitch."}])
    with mock.patch("pipeline.s10_ideas._call_extractor", fake):
        r = env.client.post(f"/api/run/{RUN}/ideas/{env.idea_id}/regenerate")

    assert r.status_code == 200
    assert (r.json()["id"], r.json()["title"]) == (env.idea_id, "Shift Swap Board")
    [idea] = _stored(env, "get_ideas", RUN)
    assert (idea["id"], idea["title"], idea["pitch"]) == (env.idea_id, "Shift Swap Board", "New pitch.")
    assert idea["regenerated_at"]
    assert _stored(env, "get_briefs", RUN, env.idea_id) == []
    prompt, cfg = calls[0]
    assert "Old Title" in prompt and "Old pitch." in prompt     # told what to steer away from
    assert (cfg["providers"][0], cfg["claude_model"]) == ("claude", "idea-model")
    [vote] = env.client.get(f"/api/run/{RUN}/feedback").json()
    assert (vote["title"], vote["current"]) == ("Old Title", False)


@pytest.mark.parametrize("reply, why", [
    (RuntimeError("claude exit 1: API 429: usage limit reached"), "usage limit"),
    ([{"id": "r1-1", "skip": True, "reason": "not software"}], "not software"),
    ([], "no usable idea"),
    ([{"id": "r1-1", "title": "old title", "pitch": "Same again."}], "repeated"),
])
def test_regenerate_failure_leaves_the_idea_and_brief_untouched(env, reply, why):
    fake, _calls = _model(reply)
    with mock.patch("pipeline.s10_ideas._call_extractor", fake):
        r = env.client.post(f"/api/run/{RUN}/ideas/{env.idea_id}/regenerate")
    assert r.status_code == 502
    assert why in r.json()["error"]
    [idea] = _stored(env, "get_ideas", RUN)
    assert (idea["title"], idea["pitch"], idea["regenerated_at"]) == ("Old Title", "Old pitch.", None)
    assert [b["problem"] for b in _stored(env, "get_briefs", RUN, env.idea_id)] == ["old problem"]


def test_regenerate_unknown_idea_is_404(env):
    r = env.client.post(f"/api/run/{RUN}/ideas/nope/regenerate")
    assert (r.status_code, r.json()) == (404, {"error": "unknown idea"})


def test_regenerate_refuses_while_the_run_is_active(env):
    store = Store(env.db)
    store.set_run_status(RUN, 10, "ideating")
    store.close()
    fake, calls = _model([{"id": "r1-1", "title": "Shift Swap Board", "pitch": "New pitch."}])
    with mock.patch("pipeline.s10_ideas._call_extractor", fake):
        r = env.client.post(f"/api/run/{RUN}/ideas/{env.idea_id}/regenerate")
    assert r.status_code == 409
    assert calls == []
    assert [i["title"] for i in _stored(env, "get_ideas", RUN)] == ["Old Title"]


def test_export_of_an_empty_feedback_set(env):
    r = env.client.get(f"/api/run/{RUN}/feedback/export")
    assert r.status_code == 200
    body = r.json()
    assert (body["run_id"], body["votes"]) == (RUN, [])
    assert body["exported_at"]


def test_export_carries_the_snapshot_and_no_ids(env):
    assert _vote(env, kind="theme", cluster_id="r1-0", verdict="nay",
                 note="consumer gripe").status_code == 200
    [v] = env.client.get(f"/api/run/{RUN}/feedback/export").json()["votes"]
    assert set(v) == GOLD_KEYS
    assert (v["kind"], v["verdict"], v["label"], v["member_pain_ids"], v["rank_at_vote"],
            v["note"]) == ("theme", "nay", "Invoices get lost", ["p1", "p2", "p3", "p4"], 1,
                           "consumer gripe")


def _gold_vote(verdict, members, at, kind="theme"):
    return {"kind": kind, "verdict": verdict, "label": "x", "title": None,
            "member_pain_ids": members, "rank_at_vote": None, "note": "", "created_at": at}


def test_score_ranking_precision_at_k_by_hand(tmp_path, capsys):
    db = str(tmp_path / "rank.sqlite")
    store = Store(db)
    members = {i: [f"t{i}-{m}" for m in range(10)] for i in range(1, 14)}
    for i in range(1, 14):
        store.save_cluster(f"r1-{i}", RUN, f"theme {i}", 10, 10, members[i])
        _rank(store, f"r1-{i}", i if i <= 12 else None, dropped=i == 13)
    store.close()
    gold = {"run_id": RUN, "votes": [
        _gold_vote("yay", members[1], "01"),
        _gold_vote("yay", members[2], "02"),
        _gold_vote("nay", members[3], "03"),
        _gold_vote("yay", members[4][:3], "04"),     # 3/10 = 0.30: exactly the bar, mapped
        _gold_vote("yay", members[5][:2], "05"),     # 2/10 = 0.20: unmapped
        _gold_vote("yay", members[6], "06"),
        _gold_vote("nay", members[6], "07"),         # newer vote on theme 6 wins
        _gold_vote("yay", ["gone-1", "gone-2"], "08"),  # no overlap anywhere: unmapped
        _gold_vote("yay", members[11], "09"),
        _gold_vote("nay", members[12], "10"),
        _gold_vote("nay", members[13], "11"),        # theme 13 is dropped: no rank
        _gold_vote("yay", members[7], "12", kind="idea"),  # ideas are not scored
    ]}
    ro = Store(db, readonly=True)
    try:
        out = score_ranking.score(gold, ro, RUN)
    finally:
        ro.close()
    # Top 10 voted: 1 yay, 2 yay, 3 nay, 4 yay, 6 nay -> 3/5.
    # Top 25 adds 11 yay, 12 nay -> 4/7. Nothing ranks below 12, so @50 equals @25.
    assert out["precision"] == {10: (pytest.approx(3 / 5), 5), 25: (pytest.approx(4 / 7), 7),
                                50: (pytest.approx(4 / 7), 7)}
    assert (out["unmapped"], out["collapsed"], out["idea_votes"]) == (2, 1, 1)
    assert [n["where"] for n in out["nays"]] == ["rank 3", "rank 6", "rank 12", "dropped"]

    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    assert score_ranking.main([str(gold_path), "--db", db]) == 0
    printed = capsys.readouterr().out
    assert "precision@10  0.600  (5 voted in top 10)" in printed
    assert "2 unmapped" in printed


def test_score_ranking_with_zero_votes_says_so(tmp_path, capsys):
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps({"run_id": RUN, "exported_at": "now", "votes": []}),
                         encoding="utf-8")
    assert score_ranking.main([str(gold_path), "--db", str(tmp_path / "absent.sqlite")]) == 0
    assert "no theme votes, nothing to score" in capsys.readouterr().out
