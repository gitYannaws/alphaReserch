# Sales Safari: code audit of the scoring half (stages 6-12)

Audited 2026-11-10, read-only, against `/home/monny/Projects/alphaResearch/sales-safari` at commit `a1f2acb` (working tree = line-ending-only changes). DB numbers are live `SELECT`s against `db/safari.sqlite`, run `pbfamily0001` (8,816 themes, 787 ranked). All paths below are relative to the `sales-safari/` directory.

Audit question: does the scoring half rank real, paid, software-solvable opportunities at the top? Short answer: it ranks **long-lived, multi-author, generically-worded complaints** at the top; "paid" is the weakest signal in the formula, "software" enters only as a yes/partial gate judged by a 14B local model, and the top of `pbfamily0001` is roughly one-third product-shaped, one-third social/venue gripe, one-third cost gripe or vague.

One caveat on the report file: `reports/pbfamily0001.md` was written 2026-08-23 10:06 -0500; commit `fbb07b3` ("Rebuild the scoring vocabularies") landed 10:18 -0500 and the DB was re-scored after it. The on-disk report is therefore one vocabulary behind the DB (rank 1: report 55.11 / demand 6.64 / WTP 5.50 `[buy, pay, paying]`; DB 67.40 / 8.12 / 8.4 `[pay for, premium, buy, money, pay, paying]`). Section 6 uses the report as asked; the DB view is given beside it.

---

## 1. The demand formula as actually computed (`pipeline/s6_demand.py`)

Input text per theme = concatenation of every member pain's `complaint, workflow_pain, workaround, wish, persona, verbatim_span` (`_texts`, s6_demand.py:95-99). Term matching = **distinct terms hit anywhere in that whole blob**, not per pain, not per author (`_term_hits`, :102-109). Pain fields reaching stage 6 come from `store.get_cluster_details` (store.py:1392-1446): also `score` (upvotes), `thread_url`, `title`, `created_at`, `source_granularity`, `author_hash`.

| Component | Source | Formula (verbatim) | Range | Weight (config.yaml:357-362) |
|---|---|---|---|---|
| `pain_intensity` | regex keyword list, whole-theme text | `min(10.0, base + len(hits) * 1.5)` with `base=3.0` (:112-114, :283) | 3.0-10.0 in 1.5 steps | 3 |
| `frequency` | count + upvotes, log curve | `voices = distinct_authors + 0.5*max(0, evidence_count - distinct_authors) + endorsement` (:288-290); `frequency = _log_scale(voices, 25)` (:291); `endorsement = min(3.0, log10(1 + sum of positive scores))` (:130-146) | 0-10, continuous | 3 |
| `willingness_to_pay` | tiered regex, whole-theme text | `min(10.0, 1.0 + 2.5 * len(strong) + 0.6 * len(weak))` (:117-127) | 1.0-10.0 | 3 |
| `reachability` | regex + granularity share | `min(10, 2.0 + 1.5*len(hits))` then `+ (post_level/evidence_count) * 2.0` (:293-295) | 2.0-10.0; 4.0 = no hits, all post-level | 2 |
| `recurrence_score` | counts: threads, workarounds, repeated 8-token keys | `min(10, _log_scale(len(threads), 12, cap=6.0) + min(2.0, workaround_count*0.6) + min(2.0, len(repeated_phrases)*0.8))` (:200-203) | 0-10 | 3 |
| `persistence_score` | dates (`created_at`) | `min(10, 1.0 + 6.0*_log_scale(span_months, 36, cap=1.0) + 3.0*(share of pains in last 12 months before corpus_latest))` (:242-247); undated fallback `min(10, 3.0 + len(hits)*1.5)` over `PERSISTENCE_TERMS` (:238-241) | 1.0-10.0 | **not in the average**; rank multiplier (:309) |

Log curve (:74-82): `cap * log10(1 + value) / log10(1 + full_at)`, clamped at `cap`. Anchors `score_full_at: {voices: 25, threads: 12, months: 36}` (config.yaml:351-354).

Aggregation (:149-151, :310): `demand_score = sum(part * weight) / sum(weights)` = `(3*int + 3*freq + 3*wtp + 2*reach + 3*rec) / 14`. Checked against the report: rank 1 `(3*6.00 + 3*8.82 + 3*5.50 + 2*4.00 + 3*8.00)/14 = 92.96/14 = 6.64`. Correct.

What is LLM-judged vs regex vs count in stage 6: **nothing is LLM-judged**. Intensity, WTP, reachability = regex lists; frequency, recurrence = counts; persistence = dates. The only LLM in the scoring half before ideas is stage 7b's yes/partial/no.

Keyword lists, verbatim:

- `INTENSITY_TERMS` (:28-38): `"can't", "cannot", "impossible", "frustrating", "frustrated", "hate", "broken", "stuck", "waste", "wasting", "pain", "annoying", "problem", "issue", "hard", "difficult", "slow", "tired of", "sick of", "desperate", "struggle", "struggling", "worst", "miserable", "terrible", "awful", "garbage", "trash", "ridiculous", "nightmare", "give up", "gave up", "useless", "hopeless"`
- `WTP_STRONG_TERMS` (:45-50): `"pay for", "paying for", "paid for", "premium", "subscription", "subscriptions", "would pay", "worth paying", "happy to pay", "worth it", "not worth", "overpriced", "rip off", "waste of money", "wasted money", "paywall", "free trial", "scam"`
- `WTP_WEAK_TERMS` (:51-54): `"pay", "paid", "paying", "price", "cost", "costs", "expensive", "cheap", "afford", "money", "budget", "buy", "bought", "fee", "fees", "dollars"`
- `REACHABILITY_TERMS` (:59-64): `"community", "forum", "forums", "sub", "subreddit", "group", "groups", "discord", "telegram", "expat", "expats", "nomad", "nomads", "meetup", "app", "apps", "tinder", "bumble", "hinge", "instagram", "youtube", "course", "class", "business"`
- `PERSISTENCE_TERMS` (fallback only, :68-71): `"still", "again", "keeps", "keep having", "after update", "workaround", "manual", "every time", "always", "still can't", "still cannot"`

Measured on the 787 ranked themes of `pbfamily0001` (DB):

| Component | sd | min-max | at base value | note |
|---|---|---|---|---|
| intensity | 1.56 | 3.0-10.0 | 321/787 (41%) at 3.0 | top firing terms: hard 94, frustrated 92, can't 78, difficult 56, problem 52, issue 35; 141/787 themes have **only** generic hits (problem/issue/hard/difficult/slow/can't) |
| frequency | 0.70 | 5.49-9.98 | - | rho with size +0.94; pinned at 10 on 0 of 8,816 themes (CLAUDE.md:270-279 fix confirmed) |
| WTP | 1.43 | 1.0-10.0 | 526/787 (67%) at 1.0 | top terms: money 143, pay 56, expensive 48, paying 38, buy 33; only 73/787 (9%) hit a STRONG term |
| reach | 1.49 | 3.75-10.0 | 497/787 (63%) at 4.0 | 4.0 = zero keyword hits + all post-level |
| recurrence | 0.75 | 3.24-8.0 | - | rho with size +0.80 |
| persistence | 1.15 | 1.47-9.36 | - | 787/787 from dates; rho with size +0.07 |
| demand | 0.68 | 3.47-8.43 | - | |

Endorsement (upvotes) among ranked: mean boost 1.37 voices, max 3.0, never zero. On a corpus with scores up to 2,990 the upvote signal is worth at most 3 "voices" out of 25, i.e. at most ~`_log_scale(3)`≈1.1 frequency points.

---

## 2. The rank formula and every gate (`pipeline/s9_rank.py`, `config.yaml:386-405`)

`rank_score = demand_score * persistence_score * solvable_weight` (s9_rank.py:113, :126; `rank_breakdown.formula` :138). Sorted `(dropped, -rank_score)` (:152). `persistence` read from `demand_scores` with `COALESCE(ds.persistence_score, 3)` (:87).

Gates, in evaluation order (:114-126):

| Gate | Rule | Config | Effect on pbfamily0001 |
|---|---|---|---|
| `min_support` | `support_reasons`: evidence_count ≥ 5, distinct_authors ≥ 4, distinct_threads ≥ 3 (:48-59; threads = distinct `thread_url or title or source_permalink` :32-36) | config.yaml:393 | insufficient_evidence 6,648; insufficient_authors 5,337; insufficient_threads 2,983 |
| `allowed_solvability` | verdict must be in `["yes", "partial"]` (:120-122); `not_evaluated` + support gap → no second reason (:116-118); if **no** valid verdict exists in the run, gate disabled with a WARNING (:101-106) | config.yaml:390 | not_rankable_solvability 1,307 (= no 1,268 + unknown 39) |
| `drop_warnings` | any tag in `["platform_meta"]` on `soft_filters.warnings` (:75, :124) | config.yaml:405 | platform_meta 389 |
| `solvable_weights` | `{"yes": 1.0, "partial": 0.6, "no": 0.25, "unknown": 0.5}` (:20; config.yaml:387), looked up by verdict (:111) | | among ranked: yes 106, partial 681; top 50: yes 45 / partial 5 |

`dropped = bool(r[4] or support_gaps or solvability_reasons or blocking)` (:125); dropped → `rank_score = 0.0`, `rank = None` (:126, :158). `soft_filters.confidence` is stored (store.py:1697) and **never read by rank** (no occurrence in s9_rank.py).

Where "prefer software" enters, and how strong:

1. Stage 7b verdict (s7b_softfilter.py:24-43), the gate above. This is the strong part: a `no` or `unknown` theme cannot rank at all. So `"no": 0.25` and `"unknown": 0.5` are **dead config** while `allowed_solvability` excludes them (the only path where they apply is the gate-disabled case, where every theme gets the same `unknown` weight, which changes nothing).
2. The multiplier, **1.0 vs 0.6** between `yes` and `partial`. That is the entire continuous preference: a 1.67x ratio.
3. Stage 10a prompt "product strategist for early-stage SOFTWARE startups" + `skip` (s10_ideas.py:16-36), stage 9b "REAL, currently-existing SOFTWARE PRODUCTS" (s9b_competitors.py:45-72). These shape the ideas, not the ranking.
4. Not in stage 6 at all: no term or signal in demand measures software-shape ("is there an app that", "I wish a tool", "I use X for this"). `app`/`apps`/`tinder` appear only in `REACHABILITY_TERMS`.

Can a non-software theme outrank a software theme? With config values:

- `no` vs `yes`: impossible under the gate (`no` is dropped, score 0). If the gate were emptied, `no` would need `demand*persistence` 4x that of the `yes` theme (0.25 vs 1.0): e.g. 9.0 x 9.0 x 0.25 = 20.25 cannot beat 5.0 x 7.0 x 1.0 = 35.0.
- `partial` vs `yes`: yes, whenever `demand*persistence` is > 1.67x. Worked example from config values: `partial` theme demand 7.0 x persistence 9.0 x 0.6 = **37.8** beats `yes` theme 5.0 x 7.0 x 1.0 = **35.0**. Real instance (DB): rank 14 "Vietnam is difficult for foreigners to date compared to other countries", 7b verdict `partial 0.80 "Cultural differences need human interaction"`, 8.43 x 8.09 x 0.6 = 40.92, ranks above 93 of the 106 `yes` themes. 679 of 681 `partial` themes score above the lowest `yes` theme (5.59).
- The practical failure is not `partial` beating `yes`; it is **social gripes being judged `yes`** (see section 6: "frustration with people" yes 0.80 "Software can manage emotions and responses."; "frustration with baseless accusations and derailment of topics" yes 0.80 "Software can track and manage discussions."; "slow response times in communication" yes 0.90; "wasting time and resources" yes 1.00). The gate is only as good as a 14B local model answering "could software solve it" (config.yaml:164 `providers: [qwen]` = `qwen2.5:14b-instruct`; s7b runs through `_call_extractor` with `extract_cfg`, s7b_softfilter.py:135), on the first 6 pains, `complaint/wish/workflow_pain` only, 240 chars each, no verbatim span (s7b:46-52).

What actually orders the ranked list (Spearman, tie-averaged, vs `rank_score`, n=787): persistence **+0.62**, demand +0.60, recurrence +0.38, intensity +0.37, size +0.34, authors +0.33, frequency +0.33, reach +0.31, **WTP +0.23** (weakest). Partitions: WTP ≥ 4 (n=72) median rank 156 vs WTP = 1 (n=526) median rank 450; intensity ≥ 6 median rank 226 vs intensity 3 median rank 519. So the final order is first "how many months does this theme span and is it recent", then "how many people and how many pain words", and only last "does anyone mention paying".

Stage order and toggles (resume.py:480-509): demand(6, hard) → softfilter(7, advisory, `soft_filter.enabled`) → rank(9, hard) → ideas(10, hard) → competitors(10, advisory, `competitors.enabled`) → reviews(10, advisory, `reviews.enabled`) → brief(11, advisory, `brief.enabled`) → report(12, hard). `only_rankable` passes `rank.min_support` into 7b (resume.py:395-396). Nothing after rank writes anything rank reads.

---

## 3. Signals of real demand that are ABSENT, and whether the data exists

| Signal | In score? | Data in DB today (pbfamily0001) |
|---|---|---|
| Background/base-rate normalisation (distinctive to this community vs everywhere) | No. Term lists were *chosen* from corpus frequencies (s6:22-27) but no per-theme score is normalised against corpus or cross-community rate. | Yes: 8 subreddits in `documents.source_url` (thepassportbros 119,781 docs; Passport_Bros 39,667; passportbrolifestyle 8,194; PassportBrosHQ 4,908; ...). Corpus-wide term counts trivially computable from `pains`. |
| Trend / burst over time | Only `share_last_12_months` (3 of 10 persistence points, s6:243-247). No acceleration, no month-over-month slope, no "new this year" flag. | Yes: `documents.created_at` on 179,533 of 186,925 docs, span 2022-07 to 2026-08. |
| Existing spend ("I pay $X for") | No. `dollars` is a WEAK term (0.6); no currency/amount regex; no product-name-as-spend detection. | Yes: raw `verbatim_span` + `raw_markdown`; `$`-amounts are greppable. |
| Workaround evidence | Weakly: `min(2.0, workaround_count*0.6)` inside recurrence (s6:202), i.e. ≤ 2 x 3/14 = 0.43 demand points. | `pains.workaround` non-empty on 1,704 of 72,344 pains (2.4%); `wish` on 9,084. |
| OP vs reply weighting | No. Every pain weighs the same; OP (problem-stating) vs reply (commentary, insults) is indistinguishable. | Derivable: permalink depth gives 9,649 submission-level vs 65,203 comment-level pains. Reddit `depth`/parent is not stored. |
| Upvote-weighted intensity | No. Upvotes only add ≤ 3 "voices" to frequency (s6:130-146). A 2,990-upvote complaint has the same intensity as a 0-upvote one. | Yes: `documents.score` on 177,130 docs, max 2,990. |
| Cross-community replication | No. Threads count (recurrence) but subreddit/community does not. | Yes: `source_url` host+sub per document; `corpora`/`sources` tables. |
| Market size proxy | No. | Partial: subreddit sizes not stored; distinct `author_hash` per sub computable. |
| Complainer is the buyer | No. `persona` text is only concatenated into the regex blob (s6:97-99). | `pains.persona_canonical` exists but has **9,631 distinct values** on this run (config `personas.max_segments: 12`) - canonicalisation did not consolidate, so it is unusable as a segment key today. |
| Emotional stance / urgency ("seeking" vs "resigned") | No. `s7c_sentiment.py` computes exactly this (LABELS :31, prompt :37-59, `urgency` 0-10) but says "NOT wired into the orchestrator" (:23-25); `resume.py` has no reference; the UI reads it (`webapp/app.py:1714-1719`). **0 rows** in `sentiments` for pbfamily0001. | Code exists; table exists (store.py:228-235, 1708-1745). |
| Platform-feature vs independent product | No. "Tinder Passport is too expensive" and "fake profiles" are both `yes`; only Tinder can fix the first. | Nothing stored; a 7b verdict value would do. |

---

## 4. Size bias and the theme-level unit

**Across all 8,816 themes** the gate dominates: `min_support` removes 91% (6,648 fail `evidence ≥ 5`), so the ranked population is the largest 9% by construction. Nothing smaller than 4 authors / 5 pains / 3 threads can appear, whatever its content.

**Among the 787 ranked themes** the formula is mildly size-positive, not size-negative: rho(size, rank_score) = +0.34; decile medians run size 7 (top) to 5 (bottom); top-20 median size 9 vs all-ranked median 6. But the size signal is compressed by the anchors: with `voices` full at 25 and the ranked population at 4-23 authors, `frequency` spans only 5.49-9.98 (sd 0.70). The maximum demand gain a 23-author theme can take over a 4-author theme is `(9.98 - 5.49) * 3/14 ≈ 0.96` points; a single STRONG WTP word is worth `2.5*3/14 = 0.54`, one more intensity word `1.5*3/14 = 0.32`, and persistence (multiplier, sd 1.15, range 1.47-9.36, rho +0.62) outweighs all of them while being size-independent (rho +0.07). Result: the three largest ranked themes (23, 22, 20 authors) sit at ranks 115, 119 and 350; the top 13 are all 5-15 authors. CLAUDE.md's "Distinct authors, not post count ... One loud user is not a market" (:145) survives only as the entry gate, not as a ranking force.

Is that bias toward small? Not toward small, but toward **old and recent-at-once**: persistence = `1 + 6*log(span_months)/log(37) + 3*recency`. A theme first raised in 2022 and still raised in 2026 gets ~9; a pain that appeared last year (a new app policy, a new scam) tops out near 4-5 regardless of volume. The formula structurally cannot surface an emerging pain.

What the theme-level unit loses:

- **Fragmentation double-counts and under-scores one pain.** 23 ranked labels mention fake profile / scam / bot (combined 171 authors; none individually above 13): ranks 3, 4, 8, 23, 63, 74, 87, 151, ... Each is scored on its fragment, and stage 10a then spent **4 of 21 idea slots** on the same product (Verify, SwipeCheck, ProfileCheck, MatchGuard: all "check a dating profile's authenticity"). 72 ranked labels mention cost/expensive/price. CLAUDE.md:153-156 says centroid merging chains into one blob; nothing in the scoring half dedupes at rank or at idea time (s10_ideas.py:283-286 takes the top 50 as-is).
- **Sub-pains inside a theme are invisible.** Scores are computed over the concatenated blob, so one member's "scam" or "premium" lights the whole theme (s6:102-114). The report's evidence line is `cluster["pains"][0]` regardless of which member carried the signal (s12_report.py:124-128), and the idea's permalink is the first pain with a permalink (s10:266-270). Rank 9 "lack of a database for digital nomads" is evidenced by "Same like Digital Nomad sounds better than broke dropshipper."
- **The 7b judge sees 6 of N pains** (s7b:46-52, SQL order), so a 23-member theme is classified on a quarter of its evidence.

---

## 5. The idea chain

How an idea is judged "buildable": (a) the stage-7b gate (`yes`/`partial`), then (b) the stage-10a model's own discretion: prompt says "If no software product can honestly address the theme, do NOT force one ... return {"id","skip":true,"reason"}" (s10_ideas.py:27-29); a theme omitted from the reply is also treated as a skip (:323-327); if the whole call fails, the deterministic keyword template is used (:328-329). There is no rubric, no evidence threshold, no "who pays" test. Candidates = `get_ranked_clusters(run_id)[:top_n*overshoot]` = top 50 (:283; config `ideas.top_n: 25`), one LLM call for all of them (:297). On the last automated run the log reads `ideas=24 (llm=24 template=0 skipped=26)` (`db/resume-pbfamily0001.log`): the model skipped 26 of 50 top-ranked themes, i.e. **half of the top 50 were not product-shaped by the drafting model's own judgement**, after they had already passed the 7b gate.

Feedback from competitors/reviews into ranking: **none**. Rank (stage 9) runs before 9b/9c/10b (resume.py:496-502). Saturation is written to `competitive_intel` for display only (s9b:215-223; store.py:1829-1843 produces the "Competition note" text) and the test pins that it must not move rank (`test_saturated_theme_is_not_penalised`, tests/test_idea_chain.py:176-185). `has_review_evidence` is stored (s10b:281-282) and shown in the UI; it does not reorder ideas, the report lists ideas by `created_at` (store.py:1931-1935), and the report does not print it. Within the chain, 10a's prompt declares `competitors:[names], review_gaps:[...]` fields (s10:18-20, :57-75), but on a fresh run those are **always empty**, because 9b runs after 10a and `competitors_run` begins with `store.clear_competitors` (s9b:162). The "WEDGE versus the named competitors" the prompt asks for (:25) is therefore drafted from world knowledge, and on a re-run it would see stale competitors of the previous iteration's ideas.

Models actually used: at commit `9571f0e` config had `model: claude-sonnet-5` for ideas/competitors/brief; the current config (`a1f2acb`) has `model: ""` for all three with the comment "blank = use extract.providers (local qwen), not the Claude plan" (config.yaml:437, 448, 476). `resume.py:405/450/464` pass that `""`, and `research_cfg` returns the config **unchanged** when `model` is falsy (extract.py:300-316), which also drops the research `SYSTEM_PROMPT`. So the next run's 9b/10a/10b execute on `qwen2.5:14b-instruct`, the exact model the project documents as having "found competitors for 8 of 642 themes and named mostly magazines" (CLAUDE.md:41-43, config.yaml:432-435). The 21 ideas in the DB (created 2026-08-23 14:57 UTC, before that config change) were made by the strong model; they are not reproducible with the current config.

Briefs with review evidence on pbfamily0001: DB (current, 21 ideas) **15 of 21 (71%)**, 22 surviving quotes; competitors 40 across 18 of 21 ideas; 722 reviews across 32 competitors. The last automated run in the log: `briefs=24 (llm=24) with_review_evidence=7` (29%); CLAUDE.md:252-254 records "7 quotes across 5 briefs" for an intermediate state. The earlier 16:42 run: `template=25`, `competitors=0`, `with_review_evidence=0`.

Quality of that evidence: reviews come from the **iTunes Search API + customer-reviews RSS only** (reviews.py:18-19), countries `[ph, us]` (config.yaml:454), `sortby=mostrecent`, rating ≤ 2. No web/G2/Capterra/Trustpilot path exists despite `competitor_sources: [web, g2, capterra, producthunt, appstore]` (config.yaml:426, read by nothing in the scoring half). Name matching is `q in track or track in q` (reviews.py:42), which on this run matched **Going → "Going Balls"** (25 reviews: "Balls Game - I don't like this game it's wayyy to easy"), **Garbo → "Garbo Night Shift"** (2 reviews: "This game was Garbo"), **Google Flights → "Google"** (25 reviews about Gemini and Safari). Those reviews are in the report under "incumbent gaps". Even correctly matched reviews are about logins, bans and ads (Bumble "OTP is never received", Tinder "banned for no reason"), not about the mined pain; the brief prompt accepts a wedge "grounded in a competitor's `weakness` or a real review quote" (s10b:47-50), and `weakness` is itself LLM-authored in 9b (s9b:64), so a wedge can be an LLM citing an LLM while still passing the quote gate.

Template fallbacks in the report: **not distinguishable**. `ideas` has no provenance column (store.py:1909-1919; columns `id, run_id, cluster_id, title, pitch, evidence_permalink, created_at`), `s12_report.py` prints title/pitch/evidence/competitors only (:141-153) and **no brief fields at all** (zero occurrences of brief/wedge/mvp/has_review_evidence in the file). The only trace of a template run is the log line `template=N` and the runner's `WARN ... DEGRADED` (resume.py:452-454). A reader could recognise a template only by its hardcoded titles: "TripFit Brief", "Streetwise Date Ledger", "Match Market Audit", "Expectation Compass", "Lifestyle Match Filter", "Signal Board" (s10:99-139, :168-173), which are passport-bros-specific strings inside a "source-agnostic" pipeline.

Pinned behaviours (tests/test_idea_chain.py): saturation never penalises rank (:176-185); `no` verdict cannot rank under the precision policy (:187-198); skipped themes are backfilled rather than shipped as refusals (:217-233); template fallback when the LLM is down (:235-242); real quote → evidenced, invented quote → dropped and not counted, no reviews → honest unproven, LLM failure → no claimed evidence (:257-312); non-product categories rejected and dead URLs dropped (:100-125). Nothing pins ranking quality, duplicate ideas, or the review-app match.

---

## 6. The top 15 of `reports/pbfamily0001.md` (verbatim labels and scores) with judgement

Report file = 2026-08-23 10:06; the "DB now" column is the same theme's current rank after the vocabulary rebuild. 7b verdict/confidence/reason are from `soft_filters` (model: qwen2.5:14b).

| # | Label (verbatim) | Rank score | Authors | DB now | 7b verdict | Judgement |
|---|---|---|---|---|---|---|
| 1 | Difficulty and cost associated with changing location on Tinder | 55.11 | 15 | 1 (67.40) | yes 0.90 "Software can change location settings." | **product-shaped, but platform-feature**: only Tinder sells Passport; the "product" is a cheaper Tinder |
| 2 | fake profiles and scams | 47.02 | 9 | 3 | yes 0.90 "Scam detection" | **product-shaped** (profile verification); 3 live incumbents |
| 3 | Airbnb pricing concerns for long-term stays | 43.67 | 6 | 15 | yes 0.90 "Can track and compare prices." | **cost-gripe**; evidence "Airbnbs don't cost more in third world countries." |
| 4 | spamming issues | 41.59 | 7 | 6 | yes 0.95 "Online platform management." | **platform-meta** ("Literally ban this spammer already??", "Stop spamming every post in this sub"); `platform_meta_share` 0.29 < 0.5 so not dropped |
| 5 | frustration with people | 41.48 | 9 | 12 | yes 0.80 "Software can manage emotions and responses." | **social-gripe** |
| 6 | Expensive flights to South America | 41.19 | 6 | - | yes | **cost-gripe**; 4 incumbents (Going, Google Flights, Hopper, Skyscanner) |
| 7 | Tinder Passport mode is ineffective or counterproductive | 40.20 | 7 | 2 (63.66) | yes 0.90 "Software can improve matching." | **product-shaped, platform-feature** (same pain as #1) |
| 8 | incorrect information in passport application | 39.99 | 9 | 27 | yes 0.90 "Correct application data." | **off-niche / vague**: Indian passport-office story; flagged `requires_in_person` |
| 9 | lack of a database for digital nomads | 39.36 | 10 | 5 | yes 0.90 "Database creation and management possible." | **vague**; evidence quote does not support the label |
| 10 | slow response times in communication | 39.19 | 9 | 9 | yes 0.90 "Software can improve communication efficiency." | **social-gripe** (dating texting: "Look at you double texting. No wonder you're single") |
| 11 | frustration with baseless accusations and derailment of topics | 39.15 | 9 | 11 | yes 0.80 "Software can track and manage discussions." | **platform-meta** (forum arguments: "Don't make assumptions dumbass"); share 0.00 |
| 12 | Tinder is full of bots | 38.90 | 9 | 4 | yes 0.90 "Detect and remove bots." | **product-shaped** (duplicate of #2) |
| 13 | a dating app targeting the people with passport bros lifestyle and women who are into it | 38.83 | 8 | 7 | yes 0.90 "Dating app development." | **product-shaped wish** (explicit "what tools do you wish existed"), but a two-sided dating marketplace |
| 14 | Financial constraints on travel | 38.81 | 5 | - | yes | **vague / cost-gripe** ("if you dont have money then it will be hard to travel overseas") |
| 15 | writing is difficult to read due to lack of punctuation and structure | 38.75 | 6 | 17 | yes 0.90 "Writing tool assistance" | **platform-meta** (insulting other posters' literacy); share 0.17 |

Tally: product-shaped 5 (#1, #2, #7, #12, #13; two are the same pain, two are a Tinder feature), cost-gripe 3 (#3, #6, #14), social-gripe 2 (#5, #10), platform-meta 3 (#4, #11, #15), vague 2 (#8, #9). The ranking does surface product-shaped pains, but at roughly one in three, and the single pain it surfaces best (profile authenticity) it surfaces four times.

Second run `reports/2a926f2fad56.md` (older formula, for contrast): rank 1 "concern about safety when traveling solo" 22.90 with `frequency 10.00 ... recurrence 10.00` pinned and `/ (1 + saturation 0.00) x solvable weight 0.60`; rank 2 "specific shared customer problem" (the placeholder-label echo CLAUDE.md:157-160 describes); rank 3 "subreddit needs more moderation"; rank 5 "As a guide I would expect more advice and less complaining". All four pathologies the design notes name are visible there and fixed in the newer run, which is fair to say.

---

## 7. Weaknesses relevant to the end goal

| Sev | Finding | Where | One-line fix |
|---|---|---|---|
| **High** | The software gate asks "could software solve it", not "would someone buy software for it"; social gripes pass as `yes` 0.8-1.0 from a 14B local model seeing 6 pains x 240 chars | s7b_softfilter.py:24-43, :46-52; config.yaml:164 | Reframe 7b as a buyer test (who pays, is it a feature only the platform can ship, is the complainer the buyer), add a `platform_feature` verdict that drops like `no`, and fold `confidence` into the weight |
| **High** | Final order is persistence-first (rho +0.62), WTP-last (+0.23); 67% of ranked themes have WTP at base; "paid" is a distinct-keyword count over a concatenated blob | s6_demand.py:117-127, :276-310; config.yaml:357-362 | Score WTP per pain (currency amounts, "I pay/paid for X", named subscriptions) as share of authors; put persistence in the weighted average or bound its multiplier (e.g. 0.75-1.25) |
| **High** | Research stages (10a/9b/10b) now run on `qwen2.5:14b` with no system prompt because `model: ""`; the project's own notes say that model names magazines | config.yaml:437, 448, 476; resume.py:405, 450, 464; extract.py:300-316 | Route the ~3 research calls per run to `codex` (already configured) or a strong model; have `research_cfg` raise or `_warn_degraded` when it resolves to a local provider |
| **High** | Fragmented themes score and draft separately: 23 ranked scam/bot/fake-profile themes, 4 of 21 ideas for one product | s10_ideas.py:283-286; s9_rank.py:152 (no merge step) | Merge candidates by label-embedding similarity before 10a (label-level, not centroid), or dedupe ideas by title/pitch similarity and backfill |
| Medium | `platform_meta` misses venue chatter: "spamming issues" 0.29, "derailment of topics" 0.00, "writing is difficult to read" 0.17, "frequent unwanted posts on home page" 0.14; vocabulary lacks spam/post/thread/OP/comment | s7_filters.py:93-104 | Add those terms, lower `PLATFORM_META_MIN_SHARE` to ~0.3, and ask 7b "is this about the venue" as a second verdict |
| Medium | 10a drafts before 9b exists: `competitors`/`review_gaps` are always `[]` on a fresh run, so the prompted "WEDGE versus the named competitors" is invented | s10_ideas.py:16-36, :57-75; resume.py:496-500 | Remove those fields from the 10a prompt (10b does it properly) or run 9b over the top-50 themes before drafting |
| Medium | App Store match by substring picks the wrong app: Going → "Going Balls", Garbo → "Garbo Night Shift", Google Flights → "Google" (52 of 722 reviews are of games/unrelated apps and are printed as incumbent gaps) | reviews.py:27-45 (:42) | Require the result's `sellerUrl` domain to equal `review_domain`, and reject `primaryGenreName == "Games"` |
| Medium | Reviews are `mostrecent` 1-2★ iOS only, [ph, us]; quotes are about logins/ads/bans, not the pain; `competitor_sources` config is read by nothing; a wedge may rest on an LLM-authored `weakness` | reviews.py:18-19; config.yaml:426; s10b_brief.py:47-50 | Rank supplied reviews by embedding similarity to the theme's pains before passing them; count `weakness`-only wedges as `has_review_evidence=0` |
| Medium | Report omits the brief, `has_review_evidence`, and idea provenance; demand/rank numbers in the `.md` go stale on re-score (report 55.11 vs DB 67.40 for the same rank-1 theme) | s12_report.py:98-153; store.py:1909-1919 | Print brief fields + evidence flag + `source` (llm/template) column on `ideas`; stamp the config hash of the scoring vocab in the report |
| Medium | Intensity/WTP/reach are "distinct terms anywhere in the theme", not normalised by members/authors; generic words (problem/issue/hard) count; 141/787 ranked themes have only generic intensity hits | s6_demand.py:102-114, :283, :293 | Score per pain, aggregate as share of distinct authors whose text hits; tier intensity like WTP |
| Medium | Nothing measures burst/novelty: persistence rewards a 3-year span (6 pts) over a 12-month surge (3 pts), so an emerging pain cannot reach the top | s6_demand.py:242-247 | Add a slope term (last-12-months rate vs prior rate) as a separate demand component |
| Low | `solvable_weights` for `no` (0.25) and `unknown` (0.5) are dead while `allowed_solvability` excludes them | config.yaml:387, :390; s9_rank.py:111, :120-122 | Delete the dead entries or document that the gate makes them moot |
| Low | `s7c_sentiment` (stance + urgency, the closest thing to a "seeking a tool" signal) is built and charted but not in `resume.py` and never run on pbfamily0001 (0 rows) | s7c_sentiment.py:23-25; store.py:1708-1745 | Wire it as an advisory stage and feed `seeking`/`angry` + urgency into demand, or delete it |
| Low | Undated sources fall back to a flat keyword persistence (3 + 1.5/hit) and `COALESCE(persistence, 3)`; a mixed-source run is not comparable across sources | s6_demand.py:238-241; s9_rank.py:87 | Normalise persistence per source or set the fallback to the run's median |
| Low | Template vocabulary is hardcoded to passport-bros ("TripFit Brief", `_CITY_WORDS`) in a source-agnostic pipeline | s10_ideas.py:92-139, :168-173 | Replace with a generic "Signal Board"-style stub and mark it as template in the DB |
| Low | Report evidence line is `pains[0]`, not the pain that carried the signal (rank 9's quote contradicts its label) | s12_report.py:124-128 | Use `scoring_evidence.*.quotes[0]` (already stored) |

What the scoring half does well:

- The log-curve fix is real: frequency and recurrence pinned at 10 on **0 of 8,816** themes (was 40%; CLAUDE.md:270-279).
- Persistence from real dates on 787/787 ranked themes, 686 distinct values, corpus-clock not wall-clock (s6:259-271).
- Competition kept out of rank (it "rewarded ignorance") is the right call and is pinned by a test.
- Every number is auditable: `scoring_evidence` stores the terms and quotes per component, `rank_breakdown` stores the formula inputs, and the report's arithmetic reproduces (`6.64 x 8.30 x 1.00 = 55.11`; `92.96 / 14 = 6.64`).
- Quote gate: containment not equality, 25-char floor, evidence counted only from surviving quotes; `_fallback_brief` is deliberately thin and says "Not generated".
- Competitor grounding: live-URL check + non-product category rejection + iTunes as a second existence check.
- Degraded paths are loud (`llm_error` in results, `DEGRADED`, `NOTIFY INCOMPLETE`), and the solvability gate degrades to "ranked without the gate" instead of emptying the run.
- `only_rankable` shares one support rule between 7b and 9 (s9_rank.py:25-41) and saved ~140 minutes without losing a ranked theme.
- Tiered WTP (strong vs weak) and the per-member `platform_meta` share are the right shapes, just under-vocabularied.
- `min_support` drop reasons are explicit and counted; `not_evaluated` is distinguished from `unknown`.
