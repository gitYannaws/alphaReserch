# Single-pass qwen extraction, verify off (2026-08-17)

Decision for the r/passportbros run and any run after it until someone changes it back.

## What changed

```yaml
verify:
  enabled: false
  require_verdict: false
```

Stage 3a runs once with `qwen2.5:14b-instruct`. Stage 3b does not run. No Claude call, no
Codex call, no second opinion. Extraction is one local model, one pass.

Both flags must move together. They are not independent:

| `require_verdict` | stage 3a writes | effect |
|---|---|---|
| `true` | `verified = NULL` | resume stops before embeddings until a judge rules |
| `false` | `verified = 1` | pains reach stages 4-12 immediately |

`store.get_pains` filters on `COALESCE(verified,1) != 0`, so only an explicit 0 is withheld.
With verify off nothing is ever set to 0, and every candidate becomes a theme input.

## The cost, stated plainly

`docs/model-extraction-benchmark-2026-07-18.md` measured qwen2.5 on the gold set at **22%
precision alone against 60% with verification**. `CLAUDE.md` puts it directly: "Turning 3b
off without re-tightening 3a leaves the pipeline with no precision gate at all."

Stage 3a uses the v3 recall prompt, which over-flags on purpose and defers precision to 3b.
Removing 3b does not make 3a stricter. Expect roughly three noise candidates for every four
extracted, all of them flowing into clustering, demand scoring and idea generation.

Read the stage 12 report with that in mind. A theme with few members is more likely to be
extraction noise than a real market.

## This is reversible without re-extracting

Verify consumes stored candidates through `store.get_unverified_pains`, which selects
`WHERE p.verified IS NULL`. Two consequences:

1. Candidates written while `require_verdict: false` carry `verified = 1`, not NULL. A later
   verify pass will NOT see them. Reset them first:

       UPDATE pains SET verified = NULL WHERE run_id = '<run_id>';

2. Then set both flags true and run:

       .venv/Scripts/python -m pipeline.extract <run_id> --verify

Extraction itself never repeats. Only the judge runs.

## Why not the qwen2.5 + qwen3 union either

`docs/model-extraction-benchmark-2026-07-16.md` recommends the free union `[qwen, qwen3]`
with `providers_mode: union` for deep niche mining: recall 32% -> 46%, false positives
2% -> 4%. It is not used here.

Reason is hardware, not quality. The models are 9.0 GB and 9.3 GB; this GPU (RTX 5070 Ti
Laptop) has 12 GB. Union alternates providers on every batch, so Ollama would evict and
reload a 9 GB model for each call. Revisit on a card with 24 GB or more.
