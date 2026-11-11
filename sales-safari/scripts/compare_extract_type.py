"""Measure stage 3a's provisional pain_type tag against the stage-3b verdict on one run.

Stage 3a tags every pain it emits with an `extract_type` inside the call it already makes.
If that tag agrees with the 3b judge often enough, a later step can skip the judge for the
pains 3a already calls non-keep. This prints the evidence for that call: a confusion
matrix of the 3a tag (rows) against the 3b verdict (columns), then precision and recall of
"3a says keep" against "3b says keep" under the configured `verify.keep_types`.

Read-only: the database is opened with mode=ro, never migrated, never written.

    .venv/Scripts/python scripts/compare_extract_type.py <run_id> [--db PATH] [--config PATH]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from pipeline.extract import PAIN_TYPES, keep_types_from_cfg  # noqa: E402
from pipeline.store import Store  # noqa: E402


def _ratio(num: int, den: int) -> str:
    return f"{num / den:.3f}" if den else "  n/a"


def render(run_id: str, rows, keep: set) -> list[str]:
    """`rows` = (extract_type, pain_type, count) as Store.extract_type_vs_verdict returns."""
    typed = sum(n for _, _, n in rows)
    judged = [(e, p, n) for e, p, n in rows if p in PAIN_TYPES]
    both = sum(n for _, _, n in judged)
    if not both:
        return [f"No overlap: no pain in run {run_id} has both a 3a extract_type and a 3b "
                f"pain_type ({typed} carry a 3a tag), so there is nothing to compare yet."]

    cells = {}
    for e, p, n in judged:
        cells[e, p] = cells.get((e, p), 0) + n
    label = "3a \\ 3b"
    first = max(len(label), *(len(t) for t in PAIN_TYPES))
    cols = [*PAIN_TYPES, "total"]
    widths = [max(len(c), 5) for c in cols]

    def line(name, values):
        return "  ".join([name.ljust(first), *(str(v).rjust(w) for v, w in zip(values, widths))])

    out = [f"Run {run_id}: 3a extract_type (rows) vs 3b pain_type (columns), over the {both} "
           f"pains that carry both.", "", line(label, cols)]
    for e in PAIN_TYPES:
        counts = [cells.get((e, p), 0) for p in PAIN_TYPES]
        out.append(line(e, [*counts, sum(counts)]))
    col_totals = [sum(cells.get((e, p), 0) for e in PAIN_TYPES) for p in PAIN_TYPES]
    out.append(line("total", [*col_totals, both]))
    excluded = sum(n for _, p, n in rows if p is not None and p not in PAIN_TYPES)
    if excluded:
        out.append(f"({excluded} tagged pains whose 3b pain_type is not one of the six types, "
                   f"such as 'unjudged', are left out of the matrix.)")

    tagged_keep = sum(n for e, _, n in judged if e in keep)
    judged_keep = sum(n for _, p, n in judged if p in keep)
    agree_keep = sum(n for e, p, n in judged if e in keep and p in keep)
    non_keep = sum(n for e, _, n in rows if e not in keep)
    share = f"{100 * non_keep / typed:.1f}%" if typed else "n/a"
    out += [
        "",
        f"Keep set (verify.keep_types): {', '.join(sorted(keep))}",
        f"  precision {_ratio(agree_keep, tagged_keep)}  {agree_keep} of {tagged_keep} pains "
        f"3a tags keep are kept by 3b.",
        f"  recall    {_ratio(agree_keep, judged_keep)}  {agree_keep} of {judged_keep} pains "
        f"3b keeps were tagged keep by 3a. Skipping 3b for 3a's non-keep tags would lose the "
        f"other {judged_keep - agree_keep}.",
        f"  non-keep  {non_keep} of {typed} pains 3a tagged ({share}) carry a non-keep tag, "
        f"the 3b calls such a skip would save.",
    ]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--db", help="SQLite file (default: db_path from --config)")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"),
                    help="config whose verify.keep_types defines the keep set")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    db = args.db or str(ROOT / cfg.get("db_path", "db/safari.sqlite"))
    store = Store(db, readonly=True)
    try:
        rows = store.extract_type_vs_verdict(args.run_id)
    finally:
        store.close()
    print("\n".join(render(args.run_id, rows, keep_types_from_cfg(cfg.get("verify")))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
