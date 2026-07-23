# Six-model gold-set benchmark under the v3 recall prompt (2026-07-18)

Follow-up to [`gold-set-findings-2026-07-16.md`](gold-set-findings-2026-07-16.md). Same 112
human-labelled docs, same `labels_hash 800e747fb53469f3`, same production prompt + span gate.
Adds six models never scored against gold labels: `sonnet`, `haiku`, and the four Codex
variants exposed in the GUI dropdown (`gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`).

> ## ⚠️ This benchmark does not answer "which extractor should we use"
>
> It was run after stage 3 was split into **3a extract (recall) + 3b verify (precision)**.
> The v3 prompt now says verbatim: *"Cast a WIDE net — your job is RECALL. A separate pass
> removes false positives later."*
>
> So the precision column below measures a quantity 3a is **designed to sacrifice**. Ranking
> extractors on it is measuring the wrong axis. The decision metric under the split
> architecture is `<model>+verify` precision — **not run here for any of the six**.
>
> What this run does establish: the v3 prompt saturates recall, and models separate on
> **span discipline** and **latency**.

> **Scope.** Docs under 500 chars only — 88.5% of extracted docs but ~69% of pain-docs. Long
> posts (11.5% of corpus, 31% of pain) are excluded. Not whole-corpus numbers.
>
> **Sample size.** 10 gold pain docs. Recall moves 10 points per document. Every CI is
> enormous. The 07-16 run measured **±6 points of run-to-run noise** on identical docs and
> labels — treat any gap under ~10 points as nothing.

## Results — 112 identical docs, prompt `extract-v3`

| config | recall | precision | F1 | span | avg/batch |
|---|---:|---:|---:|---:|---:|
| sonnet (`claude-sonnet-4-6`) | 100% [72–100] | 26% [15–41] | 41% | 90% | **17.4s** |
| codex_gpt56_luna (`gpt-5.6-luna`) | 100% [72–100] | 24% [14–39] | 39% | 90% | — ¹ |
| haiku (`claude-haiku-4-5`) | 90% [60–98] | 22% [12–37] | 35% | 89% | 31.5s |
| codex_gpt54_mini (`gpt-5.4-mini`) | 100% [72–100] | 17% [10–29] | 29% | **100%** | 100.3s |
| codex_gpt55 (`gpt-5.5`) | 100% [72–100] | 16% [9–26] | 27% | 80% | — ¹ |
| codex_gpt54 (`gpt-5.4`) | 100% [72–100] | 16% [9–27] | 27% | **100%** | 42.2s |
| qwen2.5 (cached) | 50% [24–76] | 22% [10–42] | 30% | 80% | — |
| qwen2.5+verify (cached) | 30% [11–60] | **60% [23–88]** | 40% | 67% | — |

Reweighted to the (<500 char) population: sonnet 100/26, luna 100/25, haiku 91/23,
5.4-mini 100/17, 5.5 100/16, 5.4 100/16, qwen2.5 50/24, qwen2.5+verify 26/60.

¹ Timings for luna and 5.5 were lost — the run was piped through `tail -60`, which truncated
their per-batch lines. Harness bug in the invocation, not the model. Re-measure before
using latency as a tiebreaker between those two.

Zero failures across all six models. No 429s (Codex leg and Claude leg run as separate
invocations, per the README's shared-quota warning).

## What holds

**1. The v3 recall prompt works, and saturates.** Five of six models flagged 10/10 gold pain
docs. Under v2 the same gold set had codex_sol at 60% and qwen2.5 at 40%. Recall on short
docs is now a ceiling, not a differentiator — you cannot rank models on it.

**2. Span discipline separates cleanly, and it is label-independent.** `gpt-5.4` and
`gpt-5.4-mini` produced **100% exact spans** — every span a verbatim quote, never
paraphrased. sonnet/luna 90%, haiku 89%, `gpt-5.5` 80%. This is the one column here that
does not inherit the labelling noise, and it reproduces the 07-16 finding that the Codex
line has the best span discipline.

**3. All four Codex model ids in the GUI dropdown are real.** Probed directly:
`gpt-5.6-luna` 4.0s, `gpt-5.5` 3.7s, `gpt-5.4` 3.8s, `gpt-5.4-mini` 8.5s. The Codex CLI does
not validate `-m` (no model-list subcommand, no client-side check), so these were unproven
until run. They are now proven.

**4. `gpt-5.4-mini` is a latency trap.** 100.3s average, 225.1s worst batch, 38.5 min for a
single 23-batch model — 6x sonnet for no measured quality gain. "Mini" is not the cheap
option here.

## What does not hold

**The precision column should not drive model selection.** Under `extract-v3` it measures
over-flagging, which is the prompt's stated intent. A model scoring 16% may be doing exactly
what it was told.

**No pairwise difference is established.** The top cluster (sonnet 26 / luna 24 / haiku 22)
spans 4 points against ±6 points of measured noise — indistinguishable. The gap to the
5.5/5.4 cluster (16–17) is ~9 points, still inside the noise band, though three independent
models on each side makes it marginally more than coin-flip. Suggestive, not established.

**The cached qwen2.5 rows have unverified prompt provenance.** `score_gold.py` caches scores
keyed on `labels_hash` alone — it does not record `PROMPT_VERSION`. The 07-16 doc reports
qwen2.5 at 40%/57%; the cache now holds 50%/21.7% for the same labels_hash. That shift is
consistent with a v2→v3 prompt change, but nothing in the artifact proves which prompt
produced either row. **A prompt edit silently mixes rows from two different prompts into one
table.** Fix: add `prompt_version` to the cache key.

**Everything upstream from `gold-set-findings-2026-07-16.md` still stands unresolved** — the
pain definition is unsettled, the labels contradict themselves on social complaints, and
~31% of the pain population is outside this sample's scope.

## Recommendation

1. **Do not pick an extractor from this table.** Run `+verify` on the top three
   (`sonnet+verify`, `codex_gpt56_luna+verify`, `haiku+verify`) and rank on pipeline
   precision. That is the metric the split architecture is built around, and only qwen2.5
   has a verify row today — the 22%→60% jump is measured on the weakest extractor.
2. **Split the quota pools.** 3a runs on every extracted doc; 3b runs on ~10% (candidates
   only). Both currently default to `claude -p`, so a Claude extractor starves the Claude
   judge. Putting 3a on Codex plan quota (`luna`) and leaving 3b on Claude is free headroom
   — and luna's 3a numbers are inside the noise band of sonnet's on every column.
3. **Widen the gold sample past 500 chars before benchmarking again.** Long docs are 31% of
   pain and 28–36% pain rate. Six models tied at 100% recall because the sample only contains
   the easy slice. This is now the binding constraint on every extractor question.
4. **Key `score_gold.py`'s cache on `prompt_version` as well as `labels_hash`.** Cheap fix,
   prevents silently mixing prompts across a results table.
5. **Prune `gpt-5.4-mini` from the dropdown** unless a use emerges — 6x the latency, no
   measured advantage over `gpt-5.4`, which shares its perfect span discipline.

## Reproduce

```bash
# four Codex variants (ChatGPT plan quota)
.venv/Scripts/python bench/score_gold.py \
  codex_gpt56_luna codex_gpt55 codex_gpt54 codex_gpt54_mini
# Claude leg, separate invocation - sonnet and haiku share quota and will 429 each other
.venv/Scripts/python bench/score_gold.py sonnet haiku
```

Do **not** pipe through `tail` — it buffers per-batch progress until the process exits and
the timing lines are lost.

The four `extract.codex_gpt5*` config blocks were added in this session
([`config.yaml`](../config.yaml)). Each block's key equals the webapp's extractor dropdown
value, so a bench key and a UI value are the same string; all four alias the base `codex`
command via a YAML anchor and override only `model`.
