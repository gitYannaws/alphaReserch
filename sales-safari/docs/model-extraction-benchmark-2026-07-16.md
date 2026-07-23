# Extraction model benchmark — clean rerun (2026-07-16)

> ## ⚠️ SUPERSEDED for model selection — see [`gold-set-findings-2026-07-16.md`](gold-set-findings-2026-07-16.md)
>
> This benchmark's labels come from the `pains` table, so a doc is "known-pain" only because
> a previous extractor flagged it, and "true-negative" only because it didn't.
>
> - **"Known-pain recall" = agreement with the previous extractor**, not recall.
> - **The "Random flag (FP)" column is invalid.** It counts a model wrong for flagging a doc
>   the old extractor missed. At that extractor's own 32–46% recall, ~4.5–8.2% of the
>   "true-negative" pool should be real pains — which brackets or exceeds nearly every FP
>   rate below. **A perfect extractor would score worse than qwen2.5 here**, because the
>   metric rewards under-flagging. Human labels later showed qwen was flagging martini jokes
>   and snark as market pain while scoring "2% FP".
> - Therefore the **"12% FP blocker" on promoting codex is not established**, and the
>   **union's "+44% recall at ~+1 false positive"** does not survive: on gold labels the
>   qwen2.5+qwen3 union moved recall 40%→50% while precision fell 57%→36%.
>
> Speed, cost, failure counts, and bad-span rates below are still valid — they don't depend
> on the labels.

Follow-up to [`model-extraction-benchmark-2026-07-15.md`](model-extraction-benchmark-2026-07-15.md).
That run left two gaps:

- **Claude Haiku** was rate-limited (429) after batch 7 of 12 — its result was partial.
- **ChatGPT / Codex (`gpt-5.6-terra`)** had only been used as a *judge*, never benchmarked as an extractor.

This rerun closes both, and fixes the reproducibility hole in the 07-15 run.

## What changed methodologically

The 07-15 artifacts recorded only sample **counts** (no doc IDs, no seed), and the two
model families were drawn from different samples (100-doc for Qwen, 60-doc for Claude).
That made cross-model comparison soft. This run instead uses:

- **One frozen, seeded sample** (`seed=20260716`) of **100 docs = 50 known-pain + 50 true-negative**, persisted with doc IDs in `db/finish-bench-sample.json`. Every model saw the identical docs, identical batch order.
- **True-negative pool** = docs from runs that *did* produce pains but where this doc got 0 pains (genuine negatives, not merely unprocessed docs).
- **Production span-gate, byte-identical.** The harness imports `PROMPT_HEADER`, `_span_bounds`, `_clean_field`, `_parse_json_array` straight from `pipeline/extract.py`. No reimplementation.
- **Evaluated in-memory.** Nothing was written to the `pains` table — the benchmark does not pollute production data.
- **Real calls only.** No fabricated numbers. Failures (429/timeout) are recorded, not hidden.

> ⚠️ 07-15 and 07-16 numbers are **not** cross-comparable — different samples. This run's
> docs are shorter (avg **312 chars**), so absolute coverage is lower across every model.
> Compare models **within** this table only.

Harness + artifacts (reproducible): `bench/build_sample.py`, `bench/run_model.py`,
`db/finish-bench-sample.json`, `db/finish-bench-results.json`.

## Results — all 5 models, same 100 docs

| Model | Resolved | Raw | Kept | Known-pain recall | Random flag (FP) | Bad-span rate | Core fields/item | Avg span | Speed | Cost | Failures |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Qwen 2.5 14B | `qwen2.5:14b-instruct` | 19 | 18 | 16/50 = **32%** | 1/50 = **2%** | 1/19 = **5.3%** | 1.22 | 122.9 | 5.8s | free | 0 |
| Qwen3 14B | `qwen3:14b` | 21 | 19 | 16/50 = 32% | 2/50 = 4% | 2/21 = 9.5% | 1.42 | 100.9 | 25.6s | free | 0 |
| Codex / ChatGPT | `gpt-5.6-terra` | 23 | 23 | 17/50 = **34%** | 6/50 = **12%** | 0/23 = **0%** | 1.52 | 85.0 | 10.5s | plan quota¹ | 0 |
| Claude Sonnet | `claude-sonnet-4-6` | 22 | 16 | 14/50 = 28% | 2/50 = 4% | 6/22 = **27.3%** | 1.94 | 119.5 | 22.2s | $1.00 | 0 |
| Claude Haiku | `claude-haiku-4-5-20251001` | 24 | 20 | 16/50 = 32% | 4/50 = 8% | 4/24 = 16.7% | 2.10 | 89.7 | 26.9s | $0.49 | 0 |

¹ Codex ran on signed-in ChatGPT plan quota via the CLI; per-call USD not surfaced in `--output-last-message`, so left unmeasured.

Known-pain recall = distinct known-pain docs the model flagged / 50. Random flag = distinct
true-negative docs it wrongly flagged / 50 (lower is better). Bad-span rate = items dropped
by the exact-substring gate / raw items. Exact-overlap-with-existing-spans (audit only):
qwen2.5 5, qwen3 3, codex 4, sonnet 0, haiku 2.

## Read

- **Haiku reran clean** — full 20 batches, **0 rate-limit failures** (07-15 died at batch 7). Its numbers are now trustworthy: mid-pack recall (32%), richest fields (2.10/item), but a **16.7% bad-span rate** — it paraphrases spans more than the locals — and it is the slowest model here (26.9s/call).
- **Codex (`gpt-5.6-terra`) is the standout extractor**: best known-pain recall (34%), **zero bad spans** (perfect span discipline), and 2nd-fastest. Its one weakness is the **highest false-positive rate (12%)** — it flags clean docs as pain more than any other model.
- **Sonnet is the weakest here** — lowest recall (28%), **worst bad-span rate (27.3%)**, highest cost ($1.00). Consistent with the 07-15 finding that Sonnet did not justify its quota.
- **Qwen 2.5 stays the cleanest operational default**: lowest false-positive rate (2%), lowest bad-span rate among the fast models, fastest by 2x, local/free.

## Recommendation

- **Keep `qwen2.5:14b-instruct` as the default extractor.** Unchanged from 07-15. Cleanest FP + bad-span profile, fastest, free.
- **Codex `gpt-5.6-terra` is the best cloud candidate to revisit** — top recall and zero bad spans. Blocker before promotion: its **12% false-positive rate** needs a no-pain guard (e.g. require a stronger pain signal, or a cheap second-pass reject on flagged true-negatives).
- **Haiku is viable if a deterministic span-repair pass is added** (its bad-span rate is fixable — apostrophes/whitespace/capitalization). Cheaper than Sonnet ($0.49 vs $1.00) and richer fields.
- **Do not adopt Sonnet for extraction.** Two runs now agree it under-performs both locals and Codex while costing the most.

## Ensemble follow-up — union lifts recall for free

Single-model recall topped out ~32-34%. Tested a **recall-oriented union** of the two
local models (kept by *either* model), same sample, same gate. Artifact:
`db/finish-bench-union.json`, harness `bench/union_bench.py`.

| Config | Known-pain recall | False-positive | Bad-span |
|---|---:|---:|---:|
| qwen2.5 alone (current default) | 16/50 = **32%** | 1/50 = **2%** | 5.3% |
| qwen3 alone | 16/50 = 32% | 2/50 = 4% | 9.5% |
| **UNION qwen2.5 + qwen3** | 23/50 = **46%** | 2/50 = **4%** | (gate-filtered) |
| CONSENSUS (both agree) | 9/50 = 18% | 1/50 = 2% | — |

The two models are **complementary**: each finds 16, but overlap is only 9. Union adds
**7 real pains qwen2.5 alone missed → 32% → 46% recall (+44% relative)**, and
false-positives rise by just **one doc** (2% → 4%). Consensus (intersection) is too lossy.

**Cost:** free (both local). **Only tradeoff = latency** — qwen2.5 111s + qwen3 509s ≈
620s/run vs 111s single. qwen3 is the slow anchor; a faster 2nd model would keep most of
the recall gain at lower latency.

**Updated recommendation:**
- Daily/latency-sensitive runs → **qwen2.5 single** (unchanged).
- Recall-sensitive runs (deep niche mining) → **qwen2.5 + qwen3 union**. Best free recall,
  negligible precision cost. **Shipped** as `extract.providers_mode: union` (2026-07-16):
  runs every provider, pools + dedups pains by span overlap. Enable with
  `providers: [qwen, qwen3]` + `providers_mode: union`.

## Reproduce

```bash
.venv/Scripts/python bench/build_sample.py           # writes db/finish-bench-sample.json (seed 20260716)
.venv/Scripts/python bench/run_model.py qwen2.5       # each writes into db/finish-bench-results.json
.venv/Scripts/python bench/run_model.py qwen3
.venv/Scripts/python bench/run_model.py codex
.venv/Scripts/python bench/run_model.py sonnet
.venv/Scripts/python bench/run_model.py haiku
```
Free/local models first, Claude last (Max quota). Sonnet and Haiku should not run
concurrently — both draw the same Claude quota and will 429 each other.
