# Measured evidence from the live database (db/safari.sqlite, snapshot 2026-08-23)

Queried directly by the coordinator on 2026-11-10. All numbers are from SQL over the production SQLite file, not from docs. Treat as ground truth for what the pipeline actually produced.

## Which communities were mined (the audience choice)

All runs target consumer lifestyle communities, none a professional/B2B community:
- Reddit "passport bros" subreddits (13 subs merged into run `pbfamily0001`: r/Passport_Bros 39,623 docs, r/thepassportbros 12,379, r/PassportBrosHQ 4,908, r/passportbrolifestyle 4,060, r/passportbros 3,379, ...). Topic: men travelling abroad to date. Heavy argument/ideology content.
- r/expats, r/digitalnomad, r/Philippines_Expats, r/expatsinbangkok, r/MovingToThailand (corpora exist, no scored runs).
- glp1forum.com (GLP-1 / peptide grey-market forum, 30+ sub-boards) and geomax.me (XenForo board) — collected, zero pains extracted, zero clusters.
- A synthetic corpus `synth://synth:v1` used for benchmarking (runs cb39389524e2, dd952fff8d8f).

Only two real runs ever reached a report with ideas: `pbfamily0001` (186,925 docs, 74,852 pains, 8,816 themes) and `2a926f2fad56` (r/passportbros, 3,379 docs, 1,319 pains, 197 themes).

## Stage 3b (verify / precision gate) effectively never ran on production runs

| run | pains | verified NULL (never judged) | verified=0 | verified=1 |
|---|---|---|---|---|
| pbfamily0001 | 74,852 | 72,320 (96.6%) | 2,508 | 24 |
| 2a926f2fad56 | 1,319 | 1,319 (100%) | 0 | 0 |

- On pbfamily0001 the 2,242 `verified=0, pain_type=no_pain` rows match the CLAUDE.md note about `scripts/prune_short_spans.py` (spans under 25 chars), i.e. a deterministic prune, not an LLM judgment. Only ~290 pains carry an LLM pain_type (advice_opinion 138, off_topic 66, social_complaint 62, product_friction 17, cost_complaint 7).
- `run_progress` shows stage 3 at `0/0` on both runs. `get_pains` keeps `verified IS NULL`, so every un-judged candidate flowed into clustering and ranking. CLAUDE.md's own rule says: "Turning 3b off without re-tightening 3a leaves the pipeline with no precision gate at all." That is the state of both production runs.

## Extraction field fill rates (pbfamily0001, 74,852 pains)

| field | non-empty | share |
|---|---|---|
| persona | 67,226 | 90% |
| wish | 9,340 | 12% |
| workflow_pain | 4,008 | 5% |
| workaround | 1,759 | 2% |

Workaround (the strongest Mom-Test/Sales-Safari evidence of a real, felt pain) is present on 2% of pains and is not used by any score.

## Clustering: half the pains are discarded as noise; themes are tiny

- pbfamily0001: 33,162 of 72,344 eligible pains are in a cluster (46%). 54% are HDBSCAN noise and vanish from every later stage.
- 2a926f2fad56: 856 of 1,319 clustered (65%).
- Theme size on pbfamily0001: min 2, **max 23**, mean 3.76. 6,648 of 8,816 themes (75%) have fewer than 5 members; only 5 themes have 20+ members. A 180k-document corpus produced no theme larger than 23 pains. Fragmentation, not consolidation.
- Rankable: 787 of 8,816 themes (9%) survived `min_support`; 8,029 dropped. On 2a926f2fad56: 14 of 197 ranked.

## Demand components on the 787 ranked themes (pbfamily0001)

| component | mean |
|---|---|
| pain_intensity | 4.44 |
| frequency | 6.69 |
| willingness_to_pay | 1.68 |
| reachability | 4.92 |
| recurrence | 4.83 |
| persistence | 7.25 |
| demand_score | 4.48 |

Willingness-to-pay distribution over ranked themes: 526 of 787 sit at the floor (1.0), 141 at 2.0; only 25 themes score 6 or above. WTP is effectively a constant and contributes no ranking signal.

## Software-fit verdicts (stage 7b)

- pbfamily0001: not_evaluated 6,680; no 1,268; partial 707; **yes 122**; unknown 39.
- 2a926f2fad56: no 155; partial 40; unknown 1; **yes 1**.
- Warnings fired: platform_meta 389, visa_immigration_process 143, requires_in_person 72, language_barrier 72, physical_logistics 67, banking_or_payments 25, regulated_liability 14, requires_soc2_hipaa 12, two_sided_marketplace 6, closed_api_dependency 2. Note: Tinder-dependent themes fill the top 4 ranks yet `closed_api_dependency` fired only twice in the whole run.

## Top 25 ranked themes, pbfamily0001 (rank, rank_score, demand, persistence, size, authors, solvable, label)

```
1  67.40 8.12 8.30 15 15 yes  Difficulty and cost associated with changing location on Tinder
2  63.66 7.57 8.41  7  7 yes  Tinder Passport mode is ineffective or counterproductive
3  56.91 7.42 7.67 10  9 yes  fake profiles and scams
4  51.35 6.27 8.19  9  9 yes  Tinder is full of bots
5  45.68 5.71 8.00 10 10 yes  lack of a database for digital nomads
6  45.31 5.12 8.85  7  7 yes  spamming issues
7  44.01 5.44 8.09 11  8 yes  a dating app targeting the people with passport bros lifestyle and women who are into it
8  43.95 5.70 7.71 10  9 yes  fake profiles and potential scams
9  42.49 5.54 7.67  9  9 yes  slow response times in communication
10 42.11 4.88 8.63  5  5 yes  Difficulty in finding marriage-minded partners
11 42.03 4.82 8.72  9  9 yes  frustration with baseless accusations and derailment of topics
12 41.48 5.48 7.57  9  9 yes  frustration with people
13 41.34 5.52 7.49  8  8 yes  Overpriced items
14 40.92 8.43 8.09 13 12 partial Vietnam is difficult for foreigners to date compared to other countries
15 40.35 5.22 7.73  7  6 yes  Airbnb pricing concerns for long-term stays
16 40.20 5.29 7.60 10  9 yes  Dating apps are ineffective for finding matches
17 39.52 5.10 7.75  6  6 yes  writing is difficult to read due to lack of punctuation and structure
18 39.47 5.10 7.74  7  7 yes  frequent unwanted posts on home page
19 39.05 4.93 7.92  7  7 yes  negative experience with Tinder users
20 38.26 5.17 7.40  5  5 yes  frustration with online dating for men
21 37.84 5.39 7.02  5  5 yes  warning against the apps
22 37.76 4.72 8.00  7  7 yes  unfair match distribution based on attractiveness
23 37.52 4.99 7.52  7  7 yes  fake profiles on dating apps
24 37.31 4.79 7.79 12 12 yes  misinformation and disinformation being spread
25 37.26 4.05 9.20  5  5 yes  wasting time and resources
```

Coordinator judgment per theme: ranks 1-4, 8, 16, 19, 22, 23 are complaints ABOUT Tinder (a closed incumbent; a tarpit shape: "fix dating apps"). Ranks 11, 12, 17, 18, 21, 24, 25 are forum-argument chatter, not pains. Ranks 5, 7, 10 are "build a dating app / directory for X" (two-sided marketplace tarpits). Only 13 (overpriced items, vague) and 15 (Airbnb monthly pricing) resemble a workflow-shaped software pain, and 15 is a complaint about a platform's pricing.

Note also: every one of the top 25 carries `solvable=yes` except rank 14. The LLM software-fit verdict is the dominant rank driver, and it said "yes" to:
- "frustration with people" (member spans: "Your own frustration means you are picking your own emotions as well?", "This is the problem I have with people.")
- "wasting time and resources" (member spans: "you are wasting your time and money.", "You're wasting our time and yours. Don't bother.")
- "frustration with baseless accusations and derailment of topics" (member spans: "Don't make assumptions dumbass", "cope harder.")

These are insults between forum users, extracted as pains, clustered on shared phrasing, judged software-solvable, and ranked in the top 25 of 8,816.

## Top 14 ranked themes, 2a926f2fad56 (r/passportbros only)

```
1  22.90 partial concern about safety when traveling solo
2  20.56 partial specific shared customer problem        <- placeholder-echo label
3  18.42 yes     subreddit needs more moderation          <- platform meta
4  17.17 partial Dating is difficult and expensive in a specific country
5  15.42 partial As a guide I would expect more advice and less complaining
6  15.42 partial struggling with virginity and dating in a hookup culture
7  15.42 partial insecurity related to physical appearance
8  12.98 partial Dating difficulty has increased over the years
9  12.40 partial difficulty in finding a loving partnership
10 11.45 partial Avoiding disappointment and frustration with dating apps
11 11.25 partial American women trolling and criticizing in forums
12 10.87 partial Crime and safety concerns in Medellin
13 10.87 partial Online dating apps leading to unfruitful matches
14 10.28 partial being robbed
```
Zero of 14 are software-product-shaped. The only `solvable=yes` is "subreddit needs more moderation".

## Idea chain output (pbfamily0001)

- 21 ideas drafted. 13 of 21 are dating-app products or Tinder add-ons (Verify, SwipeCheck, ProfileCheck, MatchGuard, Arrival, Compass, ShowUp, TandemDate, Intent, EvenQueue, OneMatch, WhereToSwipe, StatusShare). Others: StayClear (Airbnb pricing), SudAlert (flight alerts), Checkform (passport form check), NomadIndex (nomad directory), TripSplit (expense split), HablaTrip/RealTalk (Spanish learning), SignalMap (wifi reliability map).
- 40 competitors stored across 18 themes, all real products with live URLs (Social Catfish, TinEye, Garbo, Google Flights, Skyscanner, Hopper, Tinder, Bumble, iVisa, Splitwise, Tricount, ...). The URL gate works.
- Briefs: 15 of 21 carry a verbatim review quote (`has_review_evidence=1`), 6 do not.
- Many "incumbent weaknesses" are generic ("Not region-focused", "Deals are global, not filtered to one region"), i.e. the gap is the idea's own framing rather than evidence of unmet demand.

## Data that exists but is unused for signal

- `documents.created_at` is present on 179,533 of 179,567 docs (2022-07-14 to 2026-08-18): a four-year time series, used only for persistence span/recency, not for trend/burst.
- `documents.score` (Reddit upvotes, up to 2,990 per CLAUDE.md) exists; used only inside the intensity/frequency log scale.
- `pains.workaround`, `pains.wish`, `pains.workflow_pain`: stored, not scored.
- `documents.thread_url` + position: whether a pain is in the OP or a reply is derivable, not used.
- No background corpus: nothing measures whether a theme is distinctive to this community or generic internet grumbling.

## Working-tree state

The uncommitted diff (17 files, 5,930 insertions / 5,930 deletions) is line-ending only: `git diff -w` is empty. Last commit a1f2acb "chore: snapshot before Omarchy migration". The project has been idle since 2026-08-23.

## Config facts that explain the numbers above (config.yaml, read 2026-11-10)

- `verify.enabled: false` — "OFF since 2026-08-17 by explicit decision: one qwen pass, no second judge ... expect roughly 3 noise candidates per 4." The gold-set note in the same block: "qwen2.5 scored 22% precision alone vs 60% with verification." So production extraction precision is self-documented at roughly 22-25%: about three of four stored "pains" are noise, and they all reach clustering.
- `verify.keep_types: [product_friction, wish, cost_complaint]` — the intended precision policy exists but is inert while the gate is off.
- `cluster.min_cluster_size: 2`, `cluster_selection_method: leaf` — explains themes of 2-23 members from 72k pains; leaf selection plus size-2 minimum maximises fragmentation.
- `rank.allowed_solvability: ["yes", "partial"]` and `solvable_weights: {yes 1.0, partial 0.6, no 0.25, unknown 0.5}` — `no` is excluded from the ranked list entirely, so the LLM's "is this software-solvable" verdict is a hard gate, not an advisory multiplier. Whatever the LLM calls "yes" fills the top of the list; it called "frustration with people" and "wasting time and resources" yes.
- `rank.min_support: {evidence_count: 5, distinct_authors: 4, distinct_threads: 3}` — with themes capped at ~23 members, support is a near-constant and cannot separate a 5-author theme from a real market.
- `rank.drop_warnings: [platform_meta]` — the only hard warning; it fires per-member by share so ranks 11, 12, 17, 18, 24, 25 above (forum-argument chatter) escaped it.
- `extract.batch_size: 6`, `max_batch_chars: 10000` — extraction is tuned for a local ~4k-context Qwen; the run rows have `extract_provider`, `extract_model`, `prompt_version`, `code_version`, `config_hash` all NULL, so the production runs are not reproducible against a known prompt or model version.
