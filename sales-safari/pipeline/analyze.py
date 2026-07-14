"""Run stages 6-12 for a clustered run.

Usage:
  .venv/Scripts/python -m pipeline.analyze <run_id>
"""
import argparse
from pathlib import Path

from dotenv import load_dotenv

from pipeline.orchestrate import load_config
from pipeline.store import Store


def analyze_run(store, run_id: str, cfg: dict, root: Path = Path(".")) -> dict:
    from pipeline.s6_demand import demand_run
    from pipeline.s7_filters import filters_run
    from pipeline.s7b_softfilter import softfilter_run
    from pipeline.s8_compete import compete_run
    from pipeline.s9_rank import rank_run
    from pipeline.s9b_competitors import competitors_run
    from pipeline.reviews import reviews_run
    from pipeline.s10_ideas import ideas_run
    from pipeline.s11_validation import validation_run
    from pipeline.s12_report import report_run
    rank_weights = cfg.get("rank", {}).get("solvable_weights")

    stats = {
        "demand": demand_run(store, run_id, cfg.get("scoring_weights", {})),
        "filters": filters_run(store, run_id, cfg.get("hard_filters", [])),
    }
    if cfg.get("soft_filter", {}).get("enabled", True):
        stats["soft_filter"] = softfilter_run(store, run_id, cfg.get("extract", {}))
    stats["competition"] = compete_run(store, run_id, cfg.get("competitor_sources", []))
    stats["rank"] = rank_run(store, run_id, solvable_weights=rank_weights)
    if cfg.get("competitors", {}).get("enabled", True):
        stats["competitors"] = competitors_run(
            store, run_id,
            top_n=cfg.get("ideas", {}).get("top_n", 5),
            cover_top=cfg.get("competitors", {}).get("cover_top", 20),
            extract_cfg=cfg.get("extract", {}))
        stats["rank_after_competitors"] = rank_run(store, run_id, solvable_weights=rank_weights)
    if cfg.get("reviews", {}).get("enabled", True):
        rcfg = cfg.get("reviews", {})
        stats["reviews"] = reviews_run(
            store, run_id,
            countries=rcfg.get("countries", ["us"]),
            max_pages=rcfg.get("max_pages", 3),
            max_stars=rcfg.get("max_stars", 2),
            max_per_competitor=rcfg.get("max_per_competitor", 25))
    stats["ideas"] = ideas_run(store, run_id, cfg.get("ideas", {}).get("top_n", 5))
    stats["validation"] = validation_run(store, run_id)
    stats["report"] = report_run(store, run_id, str(root / cfg.get("report_dir", "reports")))
    return stats


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    cfg = load_config(str(cfg_path))
    root = cfg_path.resolve().parent
    store = Store(str(root / cfg.get("db_path", "db/safari.sqlite")))
    try:
        stats = analyze_run(store, args.run_id, cfg, root)
    finally:
        store.close()
    for stage, out in stats.items():
        print(f"{stage}: {out}")


if __name__ == "__main__":
    main()
