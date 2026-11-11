"""Score a run's CURRENT ranking against the owner's yay/nay votes.

    python bench/score_ranking.py db/gold-themes.json              # the run named in the file
    python bench/score_ranking.py db/gold-themes.json --run <id> --db db/safari.sqlite

The gold file comes from the web app's /api/run/{id}/feedback/export. A re-cluster reuses
cluster ids, so a vote is matched to today's theme by its member pains, never by its id:
the current theme with the largest Jaccard overlap, if that overlap is at least 0.3. Two
votes landing on one theme keep the newer verdict.

precision@k = yay / (yay + nay) among the voted themes in the top k; unrated themes are
ignored. Only theme votes are scored: an idea vote judges a drafted product, not a rank.
Pain ids are scoped to their run, so a gold file maps onto re-scores of the same run only.

The database is opened read-only; nothing is written.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.store import Store  # noqa: E402

MIN_JACCARD = 0.3
KS = (10, 25, 50)


def jaccard(a: set, b: set) -> float:
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter) if inter else 0.0


def map_votes(votes: list, themes: dict) -> tuple[dict, list]:
    """({cluster_id: (vote, jaccard)}, [unmapped vote]) against {cluster_id: {pain_id}}."""
    mapped, unmapped = {}, []
    for v in sorted(votes, key=lambda v: v.get("created_at") or ""):
        members = set(v.get("member_pain_ids") or [])
        best, score = None, 0.0
        for cid, pains in themes.items():
            j = jaccard(members, pains)
            if j > score:
                best, score = cid, j
        if best is None or score < MIN_JACCARD:
            unmapped.append(v)
        else:
            mapped[best] = (v, score)
    return mapped, unmapped


def precision_at(ranked_ids: list, verdicts: dict, k: int) -> tuple:
    """(precision or None when nothing in the top k is voted, voted count)."""
    voted = [verdicts[c] for c in ranked_ids[:k] if c in verdicts]
    return (voted.count("yay") / len(voted) if voted else None), len(voted)


def score(gold: dict, store, run_id: str) -> dict:
    theme_votes = [v for v in gold.get("votes", []) if v.get("kind") == "theme"]
    mapped, unmapped = map_votes(theme_votes, store.get_theme_members(run_id))
    rows = {r["cluster_id"]: r for r in store.get_ranked_clusters(run_id, include_dropped=True)}
    rank = {cid: r["rank"] for cid, r in rows.items()
            if not r["dropped"] and r["rank"] is not None}
    ranked_ids = sorted(rank, key=rank.get)
    verdicts = {cid: v["verdict"] for cid, (v, _j) in mapped.items()}
    nays = sorted(({"rank": rank.get(cid),
                    "where": (f"rank {rank[cid]}" if cid in rank
                              else "dropped" if cid in rows else "not ranked"),
                    "label": v.get("label"), "rank_at_vote": v.get("rank_at_vote"),
                    "jaccard": j, "note": v.get("note") or ""}
                   for cid, (v, j) in mapped.items() if v["verdict"] == "nay"),
                  key=lambda n: (n["rank"] is None, n["rank"] or 0))
    return {"theme_votes": len(theme_votes),
            "idea_votes": len(gold.get("votes", [])) - len(theme_votes),
            "mapped": len(mapped), "unmapped": len(unmapped),
            "collapsed": len(theme_votes) - len(unmapped) - len(mapped),
            "precision": {k: precision_at(ranked_ids, verdicts, k) for k in KS},
            "nays": nays}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("gold", help="exported gold JSON, e.g. db/gold-themes.json")
    ap.add_argument("--run", help="run to score (default: the run named in the gold file)")
    ap.add_argument("--db", default=str(ROOT / "db" / "safari.sqlite"))
    args = ap.parse_args(argv)

    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    run_id = args.run or gold.get("run_id")
    if not any(v.get("kind") == "theme" for v in gold.get("votes", [])):
        print(f"{args.gold}: no theme votes, nothing to score. Vote Yay/Nay on ranked "
              "themes in the web app, then export again.")
        return 0
    store = Store(args.db, readonly=True)
    try:
        out = score(gold, store, run_id)
    finally:
        store.close()

    print(f"run {run_id}: {out['theme_votes']} theme votes, {out['mapped']} mapped, "
          f"{out['unmapped']} unmapped (best Jaccard < {MIN_JACCARD})"
          + (f", {out['collapsed']} superseded by a newer vote on the same theme"
             if out["collapsed"] else "")
          + (f"; {out['idea_votes']} idea votes not scored" if out["idea_votes"] else ""))
    for k, (p, n) in out["precision"].items():
        print(f"  precision@{k:<3} " + (f"{p:.3f}  ({n} voted in top {k})" if p is not None
                                       else f"n/a    (no voted theme in top {k})"))
    print(f"nays ({len(out['nays'])}), by current rank:")
    for n in out["nays"]:
        print(f"  {n['where']:<11} was {n['rank_at_vote'] or '-':<5} J={n['jaccard']:.2f}  "
              f"{(n['label'] or '')[:70]}" + (f"  -- {n['note']}" if n["note"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
