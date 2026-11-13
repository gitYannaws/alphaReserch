Independent review of fixes 1-6 (sales-safari, a1f2acb..c1ed497), 2026-11-10.

Line numbers are at c1ed497. Another session edited `pipeline/store.py`, `webapp/app.py`, `pipeline/s10_ideas.py` and `webapp/static/index.html` in the working tree while this review ran, so working-tree line numbers differ. Every live-DB read used `mode=ro`. The re-score reproduction ran against a scratch ROOT and a scratch DB. No file under `sales-safari/` was edited.

**Defects, most severe first.**

1. **Re-clustering reuses cluster ids, and the re-score leaves every cluster-keyed row from the old themes in place.** Data integrity, high. `pipeline/cluster.py:269` mints `f"{run_id}-{lab}"`, so pass 2's theme 557 reuses the id of an unrelated old theme 557. `pipeline/store.py:1364-1370` (`clear_clusters`) deletes only `clusters` and `cluster_members`. `scripts/rescore_stages.py:143-158` never clears `ideas`, `idea_briefs`, `competitors`, `competitor_reviews` or `competitive_intel`. `rankings`, `demand_scores` and `soft_filters` stay stale until their own steps finish. Live DB right now (pass 2 done, softfilter running):

   ```
   clusters 1445 | rankings 8816 (7371 orphaned, 1445 resolve) | demand_scores 8816 | soft_filters 0
   ideas 21 (9 resolve) | idea_briefs 21 (9) | competitors 40 (14) | competitive_intel 72 (28)

   ranked list now, rank 1: score 67.4, label "Feeling inferior due to lack of attention"
   baseline rank 1:          pbfamily0001-133 "Difficulty and cost associated with changing location on Tin..."

   idea pbfamily0001-1387 "SudAlert — fare-drop alerts for flights to South A" -> "rejecting objectification and being treated as sex doll"
   idea pbfamily0001-279  "SignalMap — crowd-sourced internet reliability by"  -> "Cold approaching sucks. It's awkward as hell ..."
   competitive_intel pbfamily0001-557 saturation 2.0 "1 competing product(s) found; verify differentiation before pursuing."
                                                                         -> "Someone is criticizing feminism"
   ```

   Four readers join these rows with no guard. `store.get_ranked_clusters` (`store.py:1900`) gives the new theme the old `gap_summary`. `store.get_idea_strength` (`store.py:1650-1669`, the `/api/run/{id}/viz` ideas chart) pairs each old idea with the new theme's label, authors, demand and rank. `s9_rank.rank_run` (`s9_rank.py:106`) copies the old saturation into the new `rankings` row. `s12_report.py:30-37,122-123` lists old ideas and competitors under new themes if the report is regenerated. `rescore_stages.py:160-161` also restores `done` after a failed step, so a crash between pass 2 and rank leaves this mixed state looking finished.
   Fix: after any cluster step, delete the run's rows in `demand_scores`, `soft_filters`, `rankings`, `competitive_intel`, `ideas`, `idea_briefs`, `competitors` and `competitor_reviews`, or put a clustering generation into the cluster id so old rows cannot resolve.

2. **Pass 2 clusters pains the judge was never allowed to see.** Precision gate, high. `scripts/rescore_stages.py:147-152` calls `cluster_run`, which reads `store.get_embeddings` (`store.py:1356-1360`, `COALESCE(p.verified,1)!=0`). That returns every unjudged pain in the run, not only the slice pass 1 marked for judging. The docstring's premise that pass 1 "only exists to say which pains can reach a rankable theme" fails once the point set changes. The default `--final-min-cluster-size` of 5 (`rescore_stages.py:127-129`) is half of pass 1's 10, which widens the gap further. I rebuilt pass 1 in memory from the live embeddings and the baseline verdicts (same UMAP seed, HDBSCAN 10/1 leaf, 0.80 cohesion split). It reproduces the commit's own numbers. Then I compared it with the live pass-2 membership:

   ```
   pass-1 input (baseline verdicts): 72341
   pass-1 themes: 1488 members: 28755
   pass-1 rankable slice (approx): 28712 (log said 28643)
   cluster2 members: 27183 outside the pass-1 slice: 11132
   cluster2 themes with >=50% members outside the pass-1 slice: 573 of 1445
   ```

   Verdicts of pass-2 members (live query): 27,043 never judged, 140 kept. This is separate from the known 6% sample. Judging 100% of the slice would still leave 11,132 members (41%) unjudged, because they were outside it.
   Fix: give pass 2 only pains with `verified = 1`, or loop verify over pass 2's rankable slice until that slice holds no NULL verdict.

3. **A killed re-score leaves the marker and a non-terminal status. The next clean re-score then deletes the marker and restores that non-terminal status.** Resume and guard, medium-high. `scripts/rescore_stages.py:135-163` captures the status at start (135) and restores it unchanged in `finally` (161). Python runs `finally` on a normal exit, an exception or SIGINT. Its default SIGTERM and SIGHUP handlers exit without unwinding. Generic probe:

   ```
   SIGTERM exit=143 marker_left=yes
   SIGKILL exit=137 marker_left=yes
   SIGINT  exit=130 marker_left=no
   SIGHUP  exit=129 marker_left=yes
   ```

   The real `main()` against a scratch ROOT and DB, with the run at `12 done` and the rank step slowed by a sleep:

   ```
   after SIGTERM: exit=143 status=[9 ranked] marker_left=1
   next clean rescore: exit=0 status=[9 ranked] marker_left=0
   ```

   After the kill, `scripts/guard_pipeline.py:191-198` sees the marker and never restarts this run again, including a later real pipeline run of pbfamily0001 that dies. After the clean re-score, the run sits at `ranked` with no marker, so the guard relaunches the pipeline on its next tick. That is the incident CLAUDE.md describes. The guard is Windows-only (schtasks, PowerShell), so on this Linux machine nothing restarts, but the run is still left non-terminal.
   Fix: install SIGTERM and SIGHUP handlers that raise `SystemExit`, restore `status if status in {"done", "reported", "cancelled"} else "done"`, and write the PID into the marker so the guard can name a stale one.

4. **"not worth" counts as strong willingness to pay.** Scoring, medium. `pipeline/s6_demand.py:56`. The commit defines strong WTP as money already changing hands or a price anchor. On this corpus "not worth" is usually neither. Live pass-2 themes:

   ```
   'not worth' pains: 81; with any money/product word within the +-70/50 window: 13
     ... ppbs only exist because western women are overly promiscuous and not worth it.
     ... i get rejected all the time by girls so i quit asking them out. its not worth it.
     ... subreddit is not worth engaging with
   rankable themes: 1441; WTP raised by 'not worth': 65
     wtp 3.75 -> 7.25 (+3.50) wtp-rank 33 -> 2  "It's really not worth it."
     wtp 1.00 -> 2.20 (+1.20) wtp-rank 547 -> 98  'Juice not worth the squeeze'
     wtp 1.68 -> 2.50 (+0.82) wtp-rank 137 -> 78  'Eastern European women are not worth the hassle'
   ```

   A related point is a definition call, not a code bug. "paying for" (65 pains, the third-largest strong class) mostly reads "paying for sex", "dates for women", "her family" and "dinners". That meets the stated rule (money changed hands), but it is the "Expensive dates" confusion the old comment warned about.
   Fix: remove "not worth" from `WTP_STRONG_TERMS`, or require a money object ("not worth the money|price|subscription|fee").

5. **A 7b answer without a nested `evidence` object is silently downgraded, and the stored evidence is invented.** Stage 7b, medium-low. `pipeline/s7b_softfilter.py:97-101` builds an all-false dict whenever `evidence` is missing or is not a dict. `store.save_soft_filter` (`store.py:1717-1727`) stores it because it is not None. Probe of `_coerce`:

   ```
   nested evidence, 2 demand true   -> ('yes', 0.9, '', {'workaround': True, 'cost': True, 'recurring': False, 'payer': False, 'platform_only': False, 'venue': False})
   same fields FLAT at top level    -> ('partial', 0.9, '', {all six False})
   evidence key missing             -> ('partial', 0.9, '', {all six False})
   evidence as JSON string          -> ('partial', 0.9, '', {all six False})
   string booleans                  -> ('yes', 0.9, '', {'workaround': True, 'cost': True, ...})
   ```

   On brief item 4: a malformed batch can now produce more `partial` than before (every `yes` whose evidence did not parse), and never more `yes`. The stored JSON says the model found no workaround and no cost when it said nothing. The stage result has no counter for this, so a model that habitually flattens the object turns every `yes` into `partial` (weight 1.0 to 0.6) with no log line. How often qwen does this is unmeasured, because `soft_filters` is empty while the live step runs. String "true"/"false" is handled, extra keys are ignored, and ids from another batch are filtered at `s7b_softfilter.py:189-193`.
   Fix: when `evidence` is missing or not a dict, store NULL evidence, keep or mark the model's verdict, and count `evidence_missing` in the stage result.

6. **Momentum counts each unauthored pain as a distinct author, while stage 5 counts it as none.** Scoring, low. `pipeline/s6_demand.py:150-151` falls back to the pain id when `author_hash` is empty. `s6_demand.py:334` and the share denominators use that key. `pipeline/cluster.py:267` drops empty hashes from `distinct_authors`. Synthetic theme with 4 authored and 6 unauthored pains, all dated 2026-08-01:

   ```
   cluster.distinct_authors          : 4
   momentum recent_authors           : 10 -> momentum 9.21
   intensity share denominator       : 10
   same 4 authors without the 6 unauthored pains -> momentum 6.18
   ```

   Impact on pbfamily0001 is small. 28 clustered pains (187 in the whole DB) lack an author hash. A thread-level Firecrawl corpus has no per-post author, so its momentum would come from post count, against the "distinct authors, not post count" rule.
   Fix: return None for an empty hash and count only real authors, as `cluster.py:267` does.

**The three weakest tests.**

T1. `tests/test_theme_naming.py:72-94,130-132`. The fixture hand-writes `rank_score` 3.44 and `persistence_multiplier` 0.86, and the test asserts that the report prints them. It never calls `rank_run`, so it cannot detect the report's arithmetic drifting from rank's. "The printed arithmetic must reproduce the stored rank score" is a claim about fixture data. Fix: build the row with `rank_run` on a temp Store and assert that the printed product equals the stored score.

T2. `tests/test_demand_scale.py:36,166` (`MomentumTests`). `NOW` is 2026-08-18, 84 days before today, so a momentum measured against the wall clock passes. In-process mutant at c1ed497:

   ```
   baseline                                     exit=0 ['68 passed, 13 subtests passed']
   B: momentum measured against the WALL CLOCK  exit=0 ['68 passed, 13 subtests passed']
   ```

   Fix: put `NOW` years in the past (2020) so a wall-clock "now" empties the 12-month window.

T3. `tests/test_scoring_terms.py:26-30,91`. When there are no authors, the fixture sets `distinct_authors = len(texts)`. `cluster.py:267` never produces that state (it stores 0). Line 91 then asserts the pain-id fallback as the contract. The test pins defect 6 and hides the disagreement between frequency and momentum. Fix: set `distinct_authors` the way `cluster.py` does and assert that momentum and frequency count the same people.

Also noted. `tests/test_cluster_quality.py:125` calls `unittest.main()` before `class AuditSampleTests` (line 128). pytest collects all 7 tests, but `python tests/test_cluster_quality.py` prints "Ran 6 tests ... OK". `tests/test_buyer_gate.py:142-146` compares `rank_score` with `demand x multiplier x solvable_weight` read back from the breakdown that `rank_run` computed it from, so it confirms itself. No test covers a missing `evidence` key (defect 5). Removing the persistence clamp also passes every test, but that mutant is equivalent because persistence is already in [1, 10], so it is not a gap.

**Checked and found correct.**

- `cluster_noise` across both clusterings. 27,183 theme members all carry 0. No pain with 0 sits outside a theme, and no member carries anything but 0. The 4,167 rejected pains are NULL, because `clear_clusters` resets the run to NULL first (`store.py:1369`).
- Migrations. `soft_filters.evidence`, `pains.cluster_noise`, `clusters.cohesion` and `clusters.audit_label_source` exist in the live DB, and `_ensure_column` is idempotent.
- `author_key` collisions. Pain ids and author hashes are both 16-hex values, so a collision needs a 64-bit match. The largest author bucket (546 pains) is not a shared "[deleted]" bucket, because both Reddit collectors drop deleted authors (`arcticshift_collector.py:153`, `reddit_collector.py:29`).
- Momentum's "now". The newest clustered pain is 2026-08-17T18:55 and the newest run document is 2026-08-18T02:34, a 7.6-hour gap with no effect on a 365-day window.
- Share tags by pain rather than by author. Recomputing both tags per author flips 0 of 1,441 rankable themes.
- Regex over-match in `s7_filters`. `argument_chatter` tags 7 rankable themes, all troll or insult chatter. `platform_meta` tags 74. The themes tagged only by the new `posts?`, `comments?` and `op` patterns read as venue chatter. No wrong drop found. The one doubtful case, "banned from tinder", comes from the pre-existing `banned` pattern.
- Consequence terms fire on 295 of 27,183 clustered pains. "lost" misfires ("lost 15 pounds", "no love lost"), but the class barely moves scores.
- 7b confidence. A NULL confidence or a non-verdict becomes 0.5 (`s9_rank.py:126`). In the baseline, qwen's yes and partial confidences ran 0.4 to 1.0 and were never 0, so the "unparseable confidence halves the weight" path does not occur in practice.
- `platform_only` overrides any verdict, including `yes`, and drops the theme. That is the stated design. It is now the strongest single lever one 14B boolean has over rank, and nothing counts how often it fires. This is a judgment call, not a defect.
- 7b batch fit. The largest theme payload is about 6.8k chars, under the 12k budget. Header plus batch is about 15k chars, roughly 4k tokens at an assumed 3.7 chars per token, inside `num_ctx` 8192. This is an estimate, not a tokenizer run.
- Label-only audit sampling keeps every member. `part` is a numpy array on every path, so the centrality sort cannot fail on a list.
- Division by zero. `_log_scale`, `_share_score`, `share_tag_share` and `_cohesion` all guard empty input. `persistence_multiplier` stays in [0.75, 1.25].
- `rankable_slice` chunks its IN list at 500 ids, and `verify_run` judges only pains with a NULL verdict, so `--from verify` is idempotent.
- Tests. 251 passed on a clean `git archive` of c1ed497, including 108 in the ten changed test files.

Reproduction scripts (scratchpad, rerunnable, read-only against the live DB): `tags_probe.py`, `wtp_probe.py`, `notworth_probe.py`, `notworth_effect.py`, `conseq_probe.py`, `share_authors.py`, `cluster1_approx.py`, `coerce_probe.py`, `author_probe.py`, `finally_probe.py`, `rescore_wrap.py` and `mutants.py`, all in `/home/monny/.cache/claude-tmp/claude-1000/-home-monny-Projects-alphaResearch/c04b39a9-725d-4a94-b3bc-581b206387d8/scratchpad/`.
