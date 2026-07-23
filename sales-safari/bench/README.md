# Extraction benchmark harness

Two harnesses live here. **They measure different things — do not mix their numbers.**

| | labels come from | measures | trust |
|---|---|---|---|
| **gold** (`build_gold_sample.py` → `label.html` → `score_gold.py`) | **a human** | real precision / recall | ground truth |
| **legacy** (`build_sample.py` → `run_model.py`) | the `pains` table | *agreement with the previous extractor* | circular — see below |

Both import the **production span-gate** from `pipeline/extract.py` (not reimplemented) and
evaluate in-memory — neither ever writes to the `pains` table.

## Gold set (use this)

The legacy harness calls a doc "known-pain" only because an earlier extractor flagged it,
and "true-negative" only because an earlier extractor found nothing. Since that extractor
has 32–46% recall, its negatives are ~4.5–8.2% real pains — so its "false positive" metric
penalises a model for *finding* something, and a perfect extractor would score worse on it
than qwen2.5. Recall measured that way is agreement, not recall.

The gold set fixes this by never reading `pains`. It samples the **extracted** population
(docs a run actually put through stage 3 — 66% of the corpus never has been), stratified by
**document length**, which is independent of any extractor's opinion and correlates strongly
with pain density (3.4% → 35.9%). Each stratum carries a population weight so scores
reweight to unbiased population estimates.

```bash
.venv/Scripts/python bench/build_gold_sample.py --emit-html
#   -> db/gold-sample.json      (120 docs, BLIND)
#   -> bench/label-gold.html    (same sample inlined; just open it, no picker, no server)
# label it:  y = has pain   n = no pain   s = add selection as span   arrows = navigate
#   autosaves to localStorage; close and resume any time.
#   "Export labels" downloads gold-labels.json -> move it to db/gold-labels.json
.venv/Scripts/python bench/score_gold.py incumbent      # what the pains table already holds
.venv/Scripts/python bench/score_gold.py qwen2.5 qwen3 --union
```

`label.html` is the template and still works standalone (it shows a "Load sample…" picker).
`label-gold.html` is generated from it with the sample inlined — it makes zero network
requests, so it works from `file://`. Regenerate it whenever you re-freeze the sample;
it is derived, not a source file.

`incumbent` scores the pains already in the DB — no model calls — and is what answers
"is the 10.9% yield an extraction miss or is the collection genuinely pain-free?".

Labelling is blind on purpose: no pain flags, no existing spans, buckets shuffled. Don't
look at model output first.

## Legacy harness (superseded)

Kept because `docs/model-extraction-benchmark-2026-07-16.md` reports its numbers. Its
recall column is a *relative* agreement measure; its FP column should not be used for model
selection until re-derived from gold labels.

- `build_sample.py` — freezes a seeded 50/50 sample (known-pain + true-negative docs) to `db/finish-bench-sample.json`.
- `run_model.py <model_key>` — runs ONE extractor over that sample, appends its scored result to `db/finish-bench-results.json`.

Model keys: `qwen2.5`, `qwen3` (local Ollama), `codex` (`gpt-5.6-terra` via Codex CLI),
`sonnet`, `haiku` (Claude CLI). Model/endpoint config is read from `config.yaml` `extract:`.

## Run
```bash
.venv/Scripts/python bench/build_sample.py
.venv/Scripts/python bench/run_model.py qwen2.5
# ...one per model. Free/local first, Claude last. Don't run sonnet+haiku at once (shared quota → 429).
```

## Notes
- Sample is seeded (`SEED` in `build_sample.py`) → identical docs across models. Re-freeze only if you intend a new sample.
- Latest results write-up: `docs/model-extraction-benchmark-2026-07-16.md`.
