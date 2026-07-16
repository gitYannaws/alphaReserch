# Extraction model benchmark notes

Date: 2026-07-15  
Pipeline area: post extraction pain-span extraction  
Primary artifacts:

- `db/qwen-codex-eval-20260715-193704.json`
- `db/qwen-codex-eval-20260715-193704.out.log`
- `db/claude-extract-eval-20260715-203200.json`
- `db/claude-extract-eval-20260715-203200.out.log`

## Why this benchmark was run

We were comparing local Qwen extraction quality against Claude CLI models for the Sales Safari post-extraction step. The goal was not only "which model finds the most spans," but which one is safest for production:

- finds real pains from posts/comments,
- keeps verbatim spans exact enough for provenance,
- avoids title leaks and paraphrased spans,
- avoids flagging random/no-pain documents,
- is fast and cheap enough to run repeatedly.

## Models tested

### Local Ollama models

Installed local models at test time:

| Friendly label | Exact model | Ollama ID | Size | Notes |
|---|---|---:|---:|---|
| Qwen 2.5 14B | `qwen2.5:14b-instruct` | `7cdf5a0187d5` | 9.0 GB | Current safer default extractor candidate |
| Qwen3 14B | `qwen3:14b` | `bdbd181c33f2` | 9.3 GB | Higher recall, more span/provenance issues |
| Qwen 2.5 Coder 7B | `qwen2.5-coder:7b` | `dae161e27b0e` | 4.7 GB | Installed, not part of this extraction benchmark |

Local model storage directory:

```text
C:\Users\almig\.ollama\models
```

### Claude CLI models

Claude CLI aliases resolved as follows during smoke tests:

| Requested alias | Actual model observed | Notes |
|---|---|---|
| `sonnet` | `claude-sonnet-4-6` | No local "Sonnet 5" alias was exposed by the CLI at test time |
| `haiku` | `claude-haiku-4-5-20251001` | Run was rate-limited before completion |

Local Claude CLI auth status during this test:

```text
loggedIn: true
authMethod: claude.ai
apiProvider: firstParty
subscriptionType: max
```

No `ANTHROPIC_API_KEY` environment variable was set in the shell used for the benchmark. That means this run used the signed-in Claude account / Max subscription quota, not raw Console API-key billing.

The `cost_usd` values reported below are CLI-reported token-cost equivalents from the result payload. Treat them as useful for relative cost/computational comparison, not as proof of an extra bill. Claude subscription usage still counts against plan limits, and paid usage credits/API billing can apply only if separately enabled/accepted.

## How the Qwen test was run

Artifact: `db/qwen-codex-eval-20260715-193704.json`

Sample:

- 50 documents known to contain accepted pain spans.
- 50 random/no-pain documents.
- Batch size: 5 documents per model call.
- Outputs were passed through the same exact-span/provenance gate used by the pipeline.
- A subset of model disagreements and bad spans was judged with Codex CLI.

Codex CLI smoke test showed the judge model as:

```text
gpt-5.6-terra
```

The Codex judge reviewed 25 cases covering bad spans and model disagreements.

## Qwen results

| Metric | Qwen 2.5 14B | Qwen3 14B |
|---|---:|---:|
| Raw extracted items | 28 | 37 |
| Accepted after gates | 27 | 32 |
| Dropped after gates | 1 | 5 |
| Drop reason | 1 bad span | 5 bad spans |
| Docs with accepted pain | 27 | 31 |
| Known-pain docs found | 23/50 | 25/50 |
| Known-pain coverage | 46% | 50% |
| Random/no-pain docs flagged | 4/50 | 6/50 |
| Random flag rate | 8% | 12% |
| Exact overlap with existing spans | 3 | 1 |
| Avg core fields per item | 1.22 | 1.25 |
| Avg span length | 136.1 chars | 142.0 chars |
| Total runtime | 126.7s | 510.1s |
| Avg call time | 6.33s | 25.50s |
| Local cost | Free | Free |

Codex judge summary:

| Judged metric | Qwen 2.5 14B | Qwen3 14B |
|---|---:|---:|
| Judged cases | 8 | 17 |
| Valid pain | 8 | 14 |
| Meaning preserved | 7 | 15 |
| Title leaks | 4 | 3 |
| Repairable spans | 1 | 5 |
| Paraphrase failures | 0 | 0 |
| Not-a-pain | 0 | 1 |

Interpretation:

- Qwen3 found more accepted pains and had better known-pain coverage.
- Qwen3 also produced more exact-span failures and took about 4x longer than Qwen 2.5.
- Qwen 2.5 was the cleanest operational default: fastest, lowest bad-span rate, and lower random/no-pain flag rate.
- Qwen3 may become the better model if a deterministic span-repair/title-leak guard is added.

## How the Claude test was run

Artifact: `db/claude-extract-eval-20260715-203200.json`

Sample:

- 30 documents known to contain accepted pain spans.
- 30 random/no-pain documents.
- Batch size: 5 documents per model call.
- Same exact-span/provenance gate was applied.
- This was not Codex-judged in the same way as the Qwen disagreement run.

Important caveat:

- Sonnet completed the full 60-document sample.
- Haiku hit the Claude limit after batch 7 with: `You've hit your limit · resets 2am (America/Chicago)`.
- Therefore Haiku's result is partial and should not be treated as a final apples-to-apples comparison.

## Claude results

| Metric | Claude Sonnet alias | Claude Haiku alias |
|---|---:|---:|
| Actual model | `claude-sonnet-4-6` | `claude-haiku-4-5-20251001` |
| Run status | Full 60-doc run | Partial; rate-limited |
| Raw extracted items | 22 | 20 |
| Accepted after gates | 18 | 16 |
| Dropped after gates | 4 | 4 |
| Drop reason | 4 bad spans | 4 bad spans |
| Docs with accepted pain | 18 | 16 |
| Known-pain docs found | 13/30 | 14/30 |
| Known-pain coverage | 43.3% | 46.7% |
| Random/no-pain docs flagged | 5/30 | 2 observed before rate limit |
| Random flag rate | 16.7% | Incomplete sample; do not trust |
| Exact overlap with existing spans | 0 | 2 |
| Avg core fields per item | 1.44 | 2.44 |
| Avg span length | 102.6 chars | 111.4 chars |
| Total runtime | 231.5s | 273.8s before stopping |
| Avg call time | 19.3s | 39.1s |
| CLI-reported token-cost equivalent | ~$0.64 | ~$0.23 before stopping |
| Failures | 0 | 5 rate-limit failures |

Interpretation:

- Sonnet did not beat either local Qwen model on this benchmark.
- Sonnet had lower recall than Qwen3, similar/lower recall than Qwen 2.5, higher false-positive rate, higher bad-span rate, and consumed Claude subscription quota.
- Haiku looked interesting on known-pain recall and field richness, but the random/no-pain portion was incomplete due to rate limiting.
- Haiku needs a clean rerun before any serious decision.

## Direct comparison

| Model | Sample | Accepted pains | Known-pain recall | Random/no-pain flags | Bad-span rate | Avg speed | Cost/quota |
|---|---:|---:|---:|---:|---:|---:|---:|
| Qwen 2.5 14B | 100 docs | 27 | 23/50 = 46% | 4/50 = 8% | 1/28 = 3.6% | 6.3s/call | local/free |
| Qwen3 14B | 100 docs | 32 | 25/50 = 50% | 6/50 = 12% | 5/37 = 13.5% | 25.5s/call | local/free |
| Claude Sonnet | 60 docs | 18 | 13/30 = 43.3% | 5/30 = 16.7% | 4/22 = 18.2% | 19.3s/call | subscription quota; CLI token-cost equivalent ~$0.64 |
| Claude Haiku | partial | 16 | 14/30 = 46.7% | incomplete | 4/20 = 20% | 39.1s/call | subscription quota; CLI token-cost equivalent ~$0.23 before stop |

## Recommendation

Keep `qwen2.5:14b-instruct` as the default extractor for now.

Reasons:

- lowest bad-span rate,
- lowest random/no-pain flag rate among complete tests,
- fastest complete model,
- local/free,
- stable enough for production-style reruns.

Do not switch to Claude Sonnet based on this test.

Qwen3 is the best candidate to revisit after improving extraction gates. It found the most accepted pains and had the best recall, but needs help with exact-span repair/title-leak handling before it should replace Qwen 2.5.

Haiku 4.5 needs a clean rerun after rate limits reset. Its partial result is not enough to choose it.

## Suggested next benchmark

Run one clean, same-sample comparison after adding deterministic post-processing:

1. Exact-span repair for apostrophes, capitalization, whitespace, and small spelling/source mismatches.
2. Title-leak guard: reject spans only present in post title unless title extraction is explicitly allowed.
3. Same 100-document sample across all models.
4. Same Codex judge pass on all bad spans and model-only finds.

Models to rerun:

- `qwen2.5:14b-instruct`
- `qwen3:14b`
- `claude-haiku-4-5-20251001`
- `claude-sonnet-4-6` only if cost is acceptable

Success criteria:

- known-pain recall above Qwen 2.5 baseline,
- random/no-pain flag rate at or below 8%,
- bad-span rate below 5% after repair,
- average runtime acceptable for full pipeline runs.
