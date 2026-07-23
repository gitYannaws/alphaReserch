# Stage 3 measured against human labels — first gold-set results (2026-07-16)

First non-circular measurement of pain extraction. Supersedes the model-selection advice in
[`model-extraction-benchmark-2026-07-16.md`](model-extraction-benchmark-2026-07-16.md),
whose labels came from the `pains` table (see [Why the old benchmark misled](#why-the-old-benchmark-misled)).

**Ground truth:** 112 docs hand-labelled by the project owner, blind — no pain flags, no
existing spans, buckets shuffled. Harness: `bench/build_gold_sample.py` → `bench/label.html`
→ `bench/score_gold.py`. Artifacts: `db/gold-sample.json`, `db/gold-labels.json`,
`db/gold-results.json` (`labels_hash 800e747fb53469f3`).

> **Scope.** Docs under 500 chars only — 88.5% of extracted docs but ~69% of pain-docs.
> Long posts (11.5% of corpus, 31% of pain, 28–36% pain rate) are excluded. These are not
> whole-corpus numbers.
>
> **Sample size.** 10 gold pain docs. Every CI below is enormous. Treat all of this as
> direction, not measurement.

## Results — 112 identical docs, production prompt + span gate

| config | recall | precision | F1 | span | tp | fp | fn | tn |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| incumbent (`pains` table) | 30% [11–60] | 25% [9–53] | 27% | 67% | 3 | 9 | 7 | 93 |
| **codex_sol** (`gpt-5.6-sol`, effort=max) | **60% [31–83]** | 40% [20–64] | **48%** | **100%** | 6 | 9 | 4 | 93 |
| qwen2.5 (current default) | 40% [17–69] | **57% [25–84]** | 47% | 50% | 4 | 3 | 6 | 99 |
| qwen3 | 40% [17–69] | 33% [14–61] | 36% | 75% | 4 | 8 | 6 | 94 |
| UNION(qwen2.5+qwen3) | 50% [24–76] | 36% [16–61] | 42% | 80% | 5 | 9 | 5 | 93 |

Reweighted to the (<500 char) population: incumbent 26/25, codex_sol 58/41, qwen2.5 34/47,
qwen3 41/32, union 49/34.

`incumbent` = the pains already in the DB for those docs. No model call — it measures the
data stages 4–12 are built on, not any single model.

## What holds

**1. The `pains` table is mostly not pains.** Precision 25%: of 12 docs flagged, 3 matched.
The 9 misses are not close calls — a martini joke ("It's Friday and I'm 3 martinis down"),
pure snark ("Nothing like advice from someone who catastrophically failed"), and advice to
others ("cold approach has a very low success rate"). The prompt explicitly says to skip
"generic advice, debate, opinions, praise, or instructions unless the speaker also states
their own complaint." That rule is being ignored. Stages 4–12 cluster, score, and generate
ideas from these 7,085 rows.

**2. It is not stale data.** 69% of all pains (4,913/7,085) come from **one run**,
`9af5b27db46e`, extractor=qwen, 2026-07-15 — the same run that was wrong 5 of 8 times on
the gold docs. Only 17% of pains lack a recorded extractor. Re-extracting with today's
config would not fix this; today's config *is* the problem.

**3. codex_sol has perfect span discipline.** 100% span agreement — every span an exact
quote, never paraphrased. qwen2.5 managed 50%. This is the one result independent of the
labels, and it matches the 07-16 benchmark's finding about codex.

**4. Provenance is not recorded.** 99% of pains (7,046/7,085) have no `prompt_version`; the
column exists and `extract.py` defines `PROMPT_VERSION = "extract-v2"`, but nothing wrote it.
The 4,913-pain run cannot be attributed to a model or prompt, so it is not reproducible.
Fix before the next extraction pass.

## What does not hold

**Every CI overlaps.** codex_sol recall [31–83] vs incumbent [11–60] overlap. No pairwise
model difference here is statistically established.

**Run-to-run noise is ±6 points.** codex_sol scored precision **46% then 40%** on two runs
over identical docs and labels. Any gap under ~10 points is noise. This is measured, not
assumed.

**The recall column is soft**, because the labels are internally inconsistent (below).

## The labels contradict themselves

Three pairs of near-identical posts, opposite verdicts from the same labeller in one session:

| labelled PAIN | labelled NO PAIN |
|---|---|
| "Sweden's expensive." | "Lots of cockroaches." |
| "white women everywhere have become impossible" | "the requirements women set for men is out of control" |
| "They're not entitled like these bitches in West" | "4 weeks in Da Nang, don't remember seeing a single baddie" |

Not close calls — the same statement type, judged both ways. So for social-complaint docs the
instrument is partly a coin flip, and precision/recall inherit that.

This is a **spec defect, not a labelling defect**. The guide said "complaint, workflow
friction, costly workaround, explicit wish". Under that wording "Sweden's expensive" *is* a
complaint. Nothing pinned the line, so it drifted across 112 docs.

The failure *modes* still separate cleanly, and the labels cannot obscure that:
- **incumbent / qwen** flag jokes and snark → wrong under any definition.
- **codex_sol** flags contested social gripes → wrong only under a strict definition.

So codex_sol's true precision is likely above 40%, and the incumbent's is genuinely ~25%.

## The unresolved question, which is upstream of everything

**Is a social complaint a pain, or only friction a product could solve?**

Of the 10 gold pain docs, roughly **two** are product-shaped: the Glowforge one
("Yesterday I spent 3 LONG HOURS trying to get the correct settings for new material" —
own friction, time cost, workaround) and a dating-app one ("that's why I don't mess with
dating apps out the country no more"). The other eight are commentary about women, places,
and dating.

If only product-shaped counts, then **r/thepassportbros is largely not a pain source** — and
that is a collection finding worth more than any extractor tuning. It also means the true
pain rate is far below 8.5%, and every sample-size calculation here is optimistic.

Answering this is a prerequisite for more labelling. Relabelling against a moving bar
produces more noise, not more signal.

## Why the old benchmark misled

`bench/build_sample.py` draws labels from the `pains` table: "known-pain" = a previous
extractor flagged it; "true-negative" = it didn't. So:

- Its **recall** means *agreement with the previous extractor*, not recall.
- Its **"random flag rate (FP)"** counts a model wrong for flagging a doc the old extractor
  missed. At that extractor's own 32–46% recall, ~4.5–8.2% of the "true negative" pool
  should be real pains — which brackets or exceeds most published FP rates (qwen2.5 2%,
  qwen3 4%, sonnet 4%, haiku 8%, codex 12%). **A perfect extractor would score worse than
  qwen2.5 on that metric**, because it rewards under-flagging.

Concretely: qwen2.5's "2% false positive rate" was measured while it was flagging martini
jokes as market pain. The nine false alarms above were *in the known-pain pool by
definition*. The circularity was not a technicality — it hid the main defect.

**Do not use the 07-15/07-16 FP column for model selection.** In particular, the "12% FP
blocker" on promoting codex is not established.

## Recommendation

1. **Settle the pain definition first.** Nothing else is worth measuring until the bar
   stops moving. Encode it as explicit rules with the contradictory pairs above as test
   cases; only ~15 contested docs need revisiting, not all 112.
2. **Do not re-extract yet.** codex_sol looks best (2x incumbent recall, perfect spans) but
   its 40% precision is still a coin flip, and a full re-extraction locks in whatever
   definition is implicit in the prompt today.
3. **Fix the prompt's precision leak.** All configs flag advice/opinion despite the prompt
   forbidding it. That is a prompt problem before it is a model problem — codex_sol,
   qwen2.5 and qwen3 all do it.
4. **Record provenance** (`prompt_version`, `extract_model`) before the next pass.
5. **Union is not the free win the 07-16 doc claimed.** Recall 40%→50%, but precision
   57%→36%. On gold labels the union trades away more precision than it buys in recall —
   the opposite of that doc's "+44% recall at ~+1 false positive" conclusion, which was
   measured against the circular pool.

## Reproduce

```bash
.venv/Scripts/python bench/build_gold_sample.py --emit-html   # blind, stratified, seed 20260716
# label bench/label-gold.html -> export -> db/gold-labels.json
.venv/Scripts/python bench/score_gold.py incumbent codex_sol qwen2.5 qwen3 --union qwen2.5,qwen3
```

Scores cache per `labels_hash`, so adding a model does not re-bill the cloud ones; edit any
verdict and the cache drops rather than mixing two ground truths.
