# Signal improvement plan — maximizing pain signal from noise

Date: 2026-07-16. Living doc. Goal: maximize the count of *genuine, distinct market pains*
that survive to ranked themes, while suppressing noise (false pains, off-topic docs,
duplicate/loud-single-user signal).

## Measured funnel (baseline, whole DB)

```
documents        157,892
  → EXTRACTED     54,057   only 34% of collected docs have ever been through stage 3
  → pain-docs      5,875   10.9% of EXTRACTED docs yield a pain
  → pains          7,085
  → CLUSTERED      2,883   only 41% of pains survive stage 5  ← biggest measured leak
  → clusters         920
  → ideas             41
```

> **Correction (2026-07-16).** An earlier revision of this doc reported "3.7% of docs yield
> any pain" by dividing 5,875 pain-docs by all 157,892 documents. 103,835 of those (66%)
> have never been through extraction, so they cannot yield a pain by construction. All
> 5,875 pain-docs come from the 54,057 extracted docs — verified: zero pain-docs fall
> outside that population. The real per-extracted-doc yield is **10.9%**, ~3x the figure
> that motivated the "extraction rate is suspiciously low" framing.

Three truths this exposes:
- **A stage-5 clustering leak dumps 59% of already-extracted pains.**
- **Two thirds of collected documents have never been extracted.** Not a quality problem —
  a coverage one, and a large pool of already-paid-for text.
- **The 10.9% extraction rate is still ambiguous** — extraction miss vs genuinely pain-free
  collection. Cannot tell which without a gold set (see Leak #0).

## Guiding principle

**Recover signal you already paid for before mining new signal.** Plugging a downstream
leak (clustering drops pains you already extracted) is far cheaper than pumping more in
(collect + extract new pains). Fix leaks first, then raise input.

## Per-stage: bottleneck → lever

### Stage 1 — Collect (top of funnel, bounds everything)
- **Bottleneck:** signal density + breadth. Off-topic/thin sources dilute; a pain never
  collected can never be recovered downstream.
- **Levers:** prioritize high-complaint venues (rant subreddits, review sites, support
  forums) over generic Q&A; collect comments not just top posts; cheap keyword/heuristic
  pre-filter to skip obvious non-pain docs (raises density, cuts extraction cost); wider
  community coverage via topic discovery.
- **Measure:** pain-doc rate per source, distinct-author coverage.
- **Priority: HIGH** — but gated on gold set to know if low density is a miss or reality.

### Stage 3 — Extract (text → pains) ← the real leak, measured 2026-07-16
- **Bottleneck: PRECISION, not recall.** Against human labels the `pains` table scores
  **25% precision / 30% recall**. It flags jokes ("I'm 3 martinis down"), snark, and advice
  to others as market pain — all of which the prompt explicitly forbids. 69% of all 7,085
  pains come from a single qwen run that was wrong 5/8 on gold docs.
- **Levers:** fix the prompt's advice/opinion leak (every model tested ignores that clause —
  a prompt problem before a model problem); `codex_sol` (`gpt-5.6-sol`, effort=max) doubles
  recall to 60% with **100% span discipline**; few-shot prompt with an explicit pain
  taxonomy; record `prompt_version` (99% of pains have none).
- **~~Union~~:** ❌ the 07-16 "+44% recall at ~+1 FP" does not survive gold labels — recall
  40%→50% but precision 57%→36%. It buys less than it costs.
- **Measure:** `bench/score_gold.py` vs human labels. The old circular metric hid all of this.
- **Priority: HIGH** — but **blocked on the pain definition** (see Leak #0), because the
  labels themselves contradict on social complaints.

### Stage 5 — Cluster (pains → themes) ← the measured leak
- **Bottleneck:** 59% of pains dropped as hdbscan noise / below the distinct-author floor.
  Unknown yet whether that is lost signal or correct filtering.
- **Levers:** tune `min_cluster_size` / `min_samples` (lower = retain more); upgrade
  embeddings (bge-small → bge-large or domain-tuned); UMAP `n_neighbors`/`min_dist`;
  soft-assign noise points to nearest cluster within a distance threshold; check the
  distinct-author floor isn't dropping real single-author pains too early.
- **Measure:** intrinsic (silhouette, Davies-Bouldin — no labels needed) + a ~30-pain
  human spot-check of the noise bucket; ARI/NMI only if precise tuning is warranted.
- **Priority: HIGH** — biggest proportional leak, cheap to probe.

### Stages 6–9 — Demand / Filters / Compete / Rank (prioritization, not signal loss)
- **Bottleneck:** weight calibration. Wrong weights bury real signal under noise themes.
  Rank = demand × persistence / saturation.
- **Levers:** weight sensitivity analysis; face-validity review of top-N; verify
  competition isn't folded into demand (design rule keeps them separate until rank).
- **Measure:** face validity + small human top-N review.
- **Priority: LOW** — garbage in = ranking irrelevant; do after upstream signal is clean.

### Stages 10–11 — Ideas / Validation (output quality)
- **Bottleneck:** downstream of cluster quality (41 ideas from 920 clusters).
- **Priority: LOW** — judgment-based, last.

### Leak #0 (cross-cutting) — Measurement
- **Bottleneck:** no ground truth. "Recall" is scored vs the old extractor = circular.
- **Levers:** hand-label a gold set (has-pain + span); instrument the funnel
  per run (docs → pains → clustered → ranked) to see the steepest drop live.
- **Compounding:** label on the *same docs* across stages — extend gold-set docs with
  theme-ids later, reuse for ranking review. One growing eval asset.
- **Priority: DO FIRST** — unblocks aiming every other step.
- **Status (2026-07-16): DONE and it paid off immediately.** 112 docs hand-labelled blind;
  5 configs scored. Results + full analysis: **`gold-set-findings-2026-07-16.md`**.
  Harness: `bench/build_gold_sample.py` → `bench/label.html` → `bench/score_gold.py`.

**Why the sample is stratified, not the 50–100 random docs this doc originally said.** At
the 10.9% base rate, 100 random docs yields ~11 pain docs — a recall denominator where one
doc moves the number ~9 points, so it cannot separate a 32% extractor from a 46% one. The
gold sampler instead stratifies on **document length**, which is independent of any
extractor's opinion (so it does not reintroduce the circularity) and correlates strongly
with pain density, over the *extracted* population:

| bucket | extracted docs | share | pain rate (old extractor, a lower bound) | in sample? |
|---|---:|---:|---:|---|
| <100 | 22,314 | 41.3% | 3.4% | ✅ 40 docs |
| 100–199 | 13,133 | 24.3% | 9.5% | ✅ 40 docs |
| 200–499 | 12,376 | 22.9% | 16.5% | ✅ 40 docs |
| 500–1499 | 5,285 | 9.8% | 28.1% | ❌ excluded |
| 1500+ | 949 | 1.8% | 35.9% | ❌ excluded |

Each stratum carries a population weight, so `score_gold.py` reweights to unbiased
population estimates — verified: the reweighted estimate reproduces the true rate exactly.

**Scope limit (`MAX_LEN = 500`).** Docs ≥500 chars were dropped to keep labelling tractable
(they were 29 of 120 docs but 83% of the reading). They are 11.5% of the corpus and **31% of
all pain-docs**, and the densest part of it. So gold numbers are recall **on docs <500
chars**, not whole-corpus. Reweighting cannot recover a stratum that was never sampled.
Raise `MAX_LEN`, fix `BUCKETS`, re-freeze for a full figure.

**Actual denominator: 10 gold pain docs** — well under the ~26–37 predicted, because the
predictions assumed the old extractor's (circular) 32–46% recall was real. Every CI in the
findings doc is enormous; run-to-run model noise alone is ±6 points. The gold set separates
*failure modes* far better than it separates *models*.

**What the old benchmark can and cannot say.** `bench/run_model.py` draws its labels from
the `pains` table, so its "known-pain recall" means *agreement with the previous extractor*,
and its "random flag rate (FP)" counts a model wrong for flagging a doc the old extractor
missed. Given that extractor's own 32–46% recall, ~4.5–8.2% of that "true negative" pool is
expected to be real pains — which brackets or exceeds most of the published FP rates
(qwen2.5 2%, qwen3 4%, sonnet 4%, haiku 8%, codex 12%). **A perfect extractor would score
*worse* than qwen2.5 on that metric**, because the metric rewards under-flagging. Treat the
07-15/07-16 FP column as unusable for model selection until it is re-derived from gold
labels; in particular the "12% FP" blocker on promoting codex is not established.

## Prioritized sequence (signal per effort)

**Revised 2026-07-16 after the gold set.** The original order put clustering second and
extraction fourth. That was wrong: at 25% extraction precision, most of what reaches stage 5
is not a pain, so the "59% clustering leak" is partly stage 5 correctly discarding junk.
Tuning clustering to retain more would retain more noise.

1. ✅ **Measure** — gold set (Leak #0). Done; it re-ordered everything below.
2. **Define what a pain is.** Blocking. The labels contradict on social complaints, and only
   ~2 of 10 gold pains are product-shaped. Until this is settled, every other number moves.
3. **Extraction precision** — fix the prompt's advice/opinion leak. Biggest measured defect
   (75% of stored pains are not pains) and it poisons every downstream stage.
4. **Collection source quality** — if only product-shaped pains count, r/thepassportbros is
   largely not a pain source. Cheaper to fix than any model.
5. **Clustering leak** — only after upstream is clean, or you tune against noise.
6. **Rank calibration** — last.

## Status
- ✅ Extraction model benchmark + union mode — `model-extraction-benchmark-2026-07-16.md`.
  **Its FP column is invalid for model selection** — see below.
- ✅ Gold set built + 112 docs hand-labelled + 5 configs scored —
  **`gold-set-findings-2026-07-16.md`**. Headline: the `pains` table is ~25% precision;
  69% of all 7,085 pains come from one qwen run that was wrong 5/8 on gold docs. Stages
  4–12 are built on that.
- ⬜ **Blocked on a definition, not on tooling.** The labels contradict themselves on social
  complaints ("Sweden's expensive" = pain, "Lots of cockroaches" = not). Settle *is a social
  complaint a pain, or only friction a product could solve?* before labelling more — only
  ~2 of 10 gold pains are product-shaped, which may mean r/thepassportbros is not a pain
  source at all.
- ⬜ Stage 5 clustering leak, collection density, rank calibration.

**Re-prioritisation.** This doc ranked stage 3 MEDIUM ("lever in hand"). The gold set says
that was wrong: extraction precision ~25% means most of what reaches stage 5 is not a pain,
so the "59% clustering leak" is partly stage 5 correctly discarding junk. Fix extraction
precision before tuning clustering — and fix the definition before either.
