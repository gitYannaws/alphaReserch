# Visualizing pains, quotes and themes (2026-09-06)

Research notes for the Sales Safari GUI. The question: how to show ~75,000 extracted pains,
their verbatim quotes, ~8,800 themes and the scores that rank them, so that one founder can
pick a pain to build for. Grounded in the `pbfamily0001` run (14 subreddits, 186,925 posts,
74,852 pains, 8,816 themes, 787 ranked, 21 ideas).

Sources: the text-visualization literature, commercial feedback tools, qualitative-data
visualization guidance, and pain-mining / problem-selection frameworks. URLs are inline.
Items marked *unverified* came from search snippets, not a fetched page.

## 1. What the GUI shows today

| View | Encoding | What it does well | Weakness seen on `pbfamily0001` |
|---|---|---|---|
| Ranked themes table | rows, score columns, expanders for breakdown and evidence | Sorted, filterable, evidence one click away | Breakdown hidden behind a click per row. Near-duplicate themes appear as separate rows (4 "fake profile" themes in the top 25). |
| Theme map | scatter: distinct authors (log) vs demand, circle size = themes on that spot, colour = intensity | Puts "many people, high demand" in one corner | No labels on the marks. Cannot tell one theme from another without hover. |
| Idea strength | horizontal bars: rank score per idea, author count as text | Cheap comparison of ideas | Hides that Verify, SwipeCheck, ProfileCheck and MatchGuard are one idea. |
| Pains per month | area chart, whole run | Dated by post time, so a backfill is not a spike | One curve for 8,816 themes. The Dec 2023 peak (8,997 pains) flattens everything else. No per-theme trend. |
| Top personas | horizontal bars: pains per persona | Quick "who speaks" view | Labels are generic ("observer", "traveler", "user"). Not tied to themes. |
| Word ranking | horizontal bars by distinct authors, word or phrase | Author-based, not count-based | Single words dominate ("women", "men", "dating"). Not decision-grade. |
| Extracted pains table | persona, theme, complaint, verbatim, source link, slicers | Full provenance per pain | 72,344 rows. No way to see wording diversity inside one theme. |
| Idea cards | problem, target user, wedge, incumbents, MVP, risks, competitor review quotes | Evidence chain from theme to review | No signal for how much of the wedge rests on a mis-matched store app (see Going, Garbo). |

The table already avoids the classic mistake (word clouds) and already counts distinct
authors. The gaps are: theme-level time, theme-level composition, duplicate detection,
and a trust strip per theme.

## 2. Research findings

### 2.1 Text-visualization and topic-model research

- **Word Tree** (Wattenberg and Viégas, 2007). A keyword-in-context tree: every phrase that
  follows a chosen word, branching right. Shows the *ways* people phrase one friction.
  Needs a seed word, so it is a drill-down, not an overview.
  http://hint.fm/projects/wordtree/ (fetch failed, TLS; interactive build at
  https://www.jasondavies.com/wordtree/).
- **Phrase Nets** (van Ham, Wattenberg, Viégas, 2009). Word graph, edge width = co-occurrence
  for one pattern ("X and Y"). Turns to a hairball above a few hundred words.
  http://hint.fm/projects/phrasenet/
- **ThemeRiver / streamgraph** (Havre and Hetzler, 2000). Stacked flowing bands, width =
  theme strength per period. Readable for ~8-10 themes, not more.
  https://ieeexplore.ieee.org/document/981848/
- **LDAvis intertopic map** (Sievert and Shirley). Topics as circles placed by MDS, area =
  prevalence, side panel of relevant terms. Distances are relative only. Good for one job:
  finding near-duplicate topics.
  https://www.researchgate.net/publication/265784473_LDAvis_A_method_for_visualizing_and_interpreting_topics
- **Termite** (Chuang, Manning, Heer). Term x topic matrix, seriated so related rows sit
  together, with a saliency score. An audit tool for cluster coherence. Unmaintained.
  http://vis.stanford.edu/papers/termite (fetch failed; https://github.com/StanfordHCI/termite)
- **BERTopic visual suite.** `visualize_hierarchy` (dendrogram over topic vectors),
  `visualize_topics_over_time`, and a DataMapPlot integration (UMAP scatter with ring
  labels). Same shape as this pipeline: HDBSCAN over sentence embeddings.
  https://maartengr.github.io/BERTopic/getting_started/visualization/visualize_hierarchy.html
  https://github.com/TutteInstitute/datamapplot
- **Nomic Atlas / DataMapPlot.** A 2-D projection of every item with nested topic labels
  by zoom level. Scales to millions of points. UMAP distances are not linear.
  https://docs.nomic.ai/atlas/embeddings-and-retrieval https://datamapplot.readthedocs.io/
- **Overview** (AP, Munzner et al.). Folder-tree clustering for millions of documents, with
  tagging while browsing. Proof that cluster browsing supports real decisions. No scoring.
  https://www.cs.ubc.ca/labs/imager/tr/2014/Overview/
- **LLooM** (Stanford, CHI 2024). LLM concept induction. Concepts as rows, data slices as
  columns, each concept with explicit inclusion *criteria*. The criteria are the part this
  GUI lacks: a theme has a label but no stated rule for membership.
  https://dl.acm.org/doi/full/10.1145/3613904.3642830 https://stanfordhci.github.io/lloom/about/
- **Anthropic Clio.** Hierarchy of abstracted cluster summaries, a UMAP map, and facet
  overlays. Withholds clusters under a support threshold, the same idea as `min_support`.
  Anthropic names false-positive clustering as an open problem.
  https://www.anthropic.com/research/clio
- **Treemap, sunburst, icicle.** A controlled study found icicle and sunburst beat treemap
  for navigation, and treemap wins for comparing sibling sizes. Sunburst fails past ~5
  levels. https://arxiv.org/pdf/1908.01277

### 2.2 Commercial feedback tools: the patterns that repeat

Studied: Dovetail, Enterpret, Thematic, Chattermill, Kraftful, Unwrap.ai, Viable, Canny,
Productboard Insights, Appbot, AppFollow, GummySearch.

1. **Overview = ranked theme list with volume, trend arrow, sentiment badge.** Not a map.
   https://www.enterpret.com/guides/the-best-software-to-visualize-themes-and-frequency-in-customer-feedback
2. **Every bar drills to verbatims.** Thematic: "every theme maps back to the comments that
   created it." https://getthematic.com/insights/customize-ai-themes-customer-feedback
3. **Customers vs mentions, and impact weighting.** Enterpret: "forty mentions from your
   largest accounts and forty from trial users look identical on a frequency chart."
4. **Week-over-week theme movement with spike alerts.** Unwrap and Chattermill.
   https://www.unwrap.ai/why-unwrap https://chattermill.com/blog/customer-feedback-analytics
5. **Theme to action link.** Canny and Productboard tie a theme to a roadmap item; Canny
   weights votes by segment. https://canny.io/use-cases/feature-request-management
6. **Topic x star-rating split and version trend** in app-review tools.
   https://appbot.co/ https://appfollow.io/blog/app-store-review-analysis

GummySearch, the closest Reddit analogue, uses qualitative buckets (Pain Points, Solution
Requests, Money Talk, Hot Discussions), not scores. A third-party page reports it shuts
down and deletes data by 2026-11-30 (*unverified*). https://gummysearch.com/product/

### 2.3 Qualitative-data visualization and UX synthesis

- **Evergreen's Qualitative Chart Chooser** (22 options: diagrams, pictures, tables, quotes
  and words). Quotes are a first-class visual element.
  https://stephanieevergreen.com/wp-content/uploads/2016/11/Qualitative-Chooser-2.0.pdf
- **Ann K. Emery.** Put a verbatim next to every number. A share alone does not persuade.
  https://depictdatastudio.com/how-to-visualize-qualitative-data/
- **NN/g affinity diagramming.** Do not dismiss small clusters that hold minority views.
  Relevant to the 8,029 themes dropped at rank. https://www.nngroup.com/articles/affinity-diagram/
- **Verdinelli and Scagnoli (2013).** In qualitative journals the *matrix* is the most used
  and most versatile display. Novel graphics are rare.
  https://journals.sagepub.com/doi/10.1177/160940691301200117
- **Atomic research "nuggets"** (Tomer Sharon). The unit is observation + evidence + tags.
  A `pains` row is already a nugget. Show nuggets as a browsable unit, not as an expander.
  https://tsharon.medium.com/the-atomic-unit-of-a-research-insight-7bf13ec8fabe
- **Opportunity Solution Tree** (Teresa Torres). Outcome, then opportunities, then solutions,
  then tests. The decision layer above the theme table. https://www.producttalk.org/opportunity-solution-trees/
- **Rigor signals.** Saturation (https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0232076),
  participants vs mentions ("most brands hear from less than 1% of customers",
  https://www.alchemer.com/resources/blog/most-brands-hear-from-less-than-1-percent-customers/),
  negative case analysis (https://atlasti.com/research-hub/negative-deviant-case-analysis).
- **Against word clouds.** Font size = raw frequency, no synonymy, no phrases, no context.
  Three viewers name three different top-5 lists.
  https://www.niemanlab.org/2011/10/word-clouds-considered-harmful/
  https://getthematic.com/insights/word-clouds-harm-insights

### 2.4 Pain mining and problem selection

- **Sales Safari** (Hoy and Hillman). Notes in four quadrants: Pain, Jargon, Worldview,
  Recommendations. Listen for "easier", "faster", "finally". This is the `pain_type`,
  `wish`, `workaround` schema. https://stackingthebricks.com/podcast/ep42-what-is-sales-safari-with-eteinne-garbugli/
- **Ulwick opportunity score.** Importance + max(Importance - Satisfaction, 0). Plotted as
  importance vs satisfaction. Here `intensity` and `willingness_to_pay` stand in for
  importance. https://en.wikipedia.org/wiki/Outcome-Driven_Innovation
- **Kano.** Needs paired survey data this corpus lacks. Vocabulary only. https://www.checkbox.com/blog/kano-model
- **RICE / ICE.** One ranked bar per item. The idea-strength bars are already this. The
  value is in naming the factors so a bar can be decomposed. https://productschool.com/blog/product-fundamentals/rice-framework
- **Mom Test evidence hierarchy.** Compliments and "would you buy" are weak. Money, time
  and reputation already spent are strong. A pain with a `workaround` outranks a bare
  `wish`. https://mtlynch.io/book-reports/the-mom-test/
- **Painkiller vs vitamin.** Two axes: urgency and necessity.
  https://www.forbes.com/sites/georgedeeb/2014/07/24/is-your-startup-building-a-vitamin-or-a-painkiller/
- **App-review dashboards.** Topic volume x star split, and trend after a release. "A bad
  build produces a burst of one-star reviews within 48-72 hours."
  https://www.unwrap.ai/post/guide-to-app-store-review-analysis https://appbot.co/features/app-rating-trends/
- **Recurring vs spike.** Small-multiple sparklines with first-seen and last-seen ticks.
  https://cobbai.com/blog/support-anomaly-detection

## 3. Recommended visual system for this GUI

Ordered by value. Each item names the fields, the encoding, the decision it supports, and
the change needed. All charts stay vanilla SVG in `webapp/static/index.html`, as now.

**Built on 2026-09-06:** 3.1, 3.2, 3.3 and 3.5. The signal row (glyph, sparkline, badges)
reads the member rows that `/cluster-details` already sends for the visible page, so no
new endpoint was needed. The quadrant replaced the theme map; `get_viz_themes` now returns
`wtp` and `rank`, and includes every theme that survived rank on top of the author cap.

### 3.1 Sub-score glyph bar in every ranked-theme row

- Fields: `demand_scores.pain_intensity, frequency, willingness_to_pay, reachability,
  recurrence_score, persistence_score`.
- Encoding: one 6-segment horizontal bar per row, fixed segment order, fixed colours, height
  ~8 px. Hover names the segment.
- Decision: see *why* a theme ranks where it does without a click. High frequency with low
  WTP is a vitamin. High WTP with low frequency is a niche painkiller.
- Change: `/api/run/{job}/rankings` already returns the parts. Render only.

### 3.2 Trust strip per theme

- Fields: `clusters.size`, `clusters.distinct_authors`, size / authors ratio, distinct
  subreddits (from `documents.thread_url`), first and last `pains.created_at`,
  `rankings.dropped`, `soft_filters.solvable`, `soft_filters.warnings`, share of
  `pains.pain_type` in {product_friction, cost_complaint}.
- Encoding: a row of small badges under the theme label. Red badge when size / authors is
  above 1.5 (a few posters inflate the theme), when one subreddit supplies over 80%, or when
  the last pain is older than 12 months.
- Decision: trust-gate a theme before you spend build time on it.
- Change: one new endpoint that aggregates per cluster. Cache it like `word-ranking`.

### 3.3 Per-theme sparkline with first-seen and last-seen ticks

- Fields: `pains.created_at` grouped by month, per `cluster_id`, for the ranked themes.
- Encoding: a 120 x 24 px sparkline in the table row. Two ticks mark first and last month.
  A flat line with one spike reads as one viral thread. A line with many bumps reads as
  recurring.
- Decision: drop themes whose score rests on one thread. This is `recurrence_score` and
  `persistence_score` as a picture.
- Change: extend `store.get_pain_timeline` to take a cluster id list. Return
  `{cluster_id: [[month, n], ...]}` for the visible page only.

### 3.4 Pain-type composition bar per theme

- Fields: `pains.pain_type` per cluster member.
- Encoding: a stacked 100% bar, five fixed colours: product_friction, cost_complaint,
  social_complaint, advice_opinion, no_pain. Note: on `pbfamily0001` only 2,532 pains have
  a type, because verify is off. Show "untyped" grey when the type is NULL.
- Decision: deprioritize themes that are venting or advice, even when the rank score is high.
  "frustration with people" and "spamming issues" fall out here.
- Change: needs verify on, or a cheap type-only pass. Without it the bar is grey.

### 3.5 Opportunity quadrant (painkiller vs vitamin)

- Fields: x = `willingness_to_pay`, y = `pain_intensity`, size = `distinct_authors`,
  colour = `soft_filters.solvable`, label = theme label for the top 30.
- Encoding: scatter with four labelled corners: painkiller (high, high), vitamin (low, low),
  niche but desperate (low WTP, high intensity), nice-to-have (high WTP, low intensity).
  Click a mark to filter the pains table.
- Decision: which themes are worth a product at all. This replaces the current theme map,
  which plots demand (a blend) against authors and has no labels.
- Change: same endpoint as the theme map. Add labels for the top N by rank.

### 3.6 Theme neighbourhood map for duplicate detection

- Fields: centroid of `embeddings.vec` per cluster (mean of member vectors), `size`,
  `rankings.rank`.
- Encoding: UMAP to 2-D over the ~800 ranked centroids only. Mark size = authors. Ring
  labels (DataMapPlot style) for the top 40. Marks whose centroids sit within a cosine
  distance of 0.15 draw a thin link.
- Decision: merge near-duplicate themes before ideas. On `pbfamily0001` this would have
  joined "fake profiles and scams", "Tinder is full of bots", "fake profiles and potential
  scams" and "fake profiles on dating apps", and cut 4 ideas to 1.
- Change: compute centroids and 2-D coordinates once at stage 5, store them on `clusters`.
  Also expose the linked pairs as a table, because the table is the actionable form.

### 3.7 Verbatim drawer with wording diversity, replacing the flat evidence expander

- Fields: `pains.complaint, verbatim_span, workaround, wish, persona, source_permalink,
  created_at`, one row per member.
- Encoding: for a selected theme, a side drawer. Rows sorted for wording diversity (greedy:
  pick the member farthest in embedding space from the ones already shown), not by recency.
  Each row shows the verbatim in quotes, the complaint below in smaller type, the workaround
  and wish as chips, the persona and date as a grey line, and a source link. One row is
  marked "dissenting": the member farthest from the centroid.
- Decision: check that the cluster is one complaint in many words, not a grab-bag.
  Read the strongest evidence (workaround present) first.
- Change: `cluster-details` already returns members. Add the distance ordering server side.

### 3.8 Persona x theme heat matrix

- Fields: `pains.persona` x `cluster_id`, cell = distinct authors.
- Encoding: rows = top 12 persona segments (`personas.max_segments`), columns = top 30
  ranked themes, cell colour = distinct authors, sequential scale. Column headers rotated.
- Decision: pick a wedge. A theme concentrated in one persona is an easier first segment.
- Change: one grouped query. Personas need better segment labels first: "observer",
  "traveler" and "user" carry 15,196 pains and say nothing.

### 3.9 Icicle of the theme hierarchy

- Fields: a coarse cut of the cluster tree (HDBSCAN condensed tree, or agglomerative over
  centroids) above the 8,816 leaves. Size = distinct authors. Colour = rank decile.
- Encoding: icicle, not sunburst. Depth 3 to 4. Dropped themes stay visible in grey.
- Decision: navigate 8,816 themes by region instead of by a flat table, and see which
  meta-topic holds several strong sub-themes. Keeps the dropped 8,029 visible, per NN/g.
- Change: store a parent id per cluster at stage 5.

### 3.10 Idea triangulation strip

- Fields: per idea, `clusters.distinct_authors` of its theme, count of `competitor_reviews`
  whose body matches the theme (the brief stage already cites these), number of competitors
  with a verified store app.
- Encoding: two short bars side by side on each idea card: Reddit authors, and competitor
  reviews that name the same pain. A red flag when the matched store app name does not
  contain the competitor name (Going vs "Going Balls").
- Decision: do not fund an idea on Reddit chatter alone. Require the pain to show up in
  people who pay for a competitor today.
- Change: the review stage must record the store app name it matched (it does: 
  `competitor_reviews.app_name`). Add the name check in `pipeline/reviews.py`.

### Improvements to what exists

- **Pains per month.** Keep, but add a toggle to log scale, and overlay the selected theme
  as a second line. The Dec 2023 peak needs an annotation with its cause once known.
- **Word ranking.** Keep the author-based ranking. Default to phrases, hide single words
  under 5 letters, and add a stop list for the community's own name ("passport bros").
- **Idea strength.** Group bars by parent theme after 3.6 exists, so duplicates stack.
- **Theme map.** Retire in favour of 3.5, or add labels for the top 20.

## 4. Rigor checklist next to every theme

1. Distinct authors and total pains, as two numbers, never one.
2. Pains per author ratio, flagged above 1.5.
3. Distinct subreddits, flagged when one supplies over 80%.
4. First-seen and last-seen month, flagged when last-seen is over 12 months old.
5. Min-support status: passed or dropped, shown, not hidden.
6. Pain-type composition, or "untyped" when verify was off.
7. Solvability verdict and warning tags as text.
8. One dissenting member (farthest from the centroid).
9. Persona concentration: share of authors in the top persona.
10. External corroboration: number of competitor reviews that name the same pain.

## 5. What not to do

- No word cloud. Font size cannot show synonyms, phrases or context. (Nieman Lab, Thematic.)
- No ranking by raw mention count. Pair every count with distinct authors. (Alchemer.)
- No single blended score as the only explanation. Decompose it. (Verdinelli and Scagnoli.)
- No streamgraph with more than ~10 bands. Use small multiples. (Havre and Hetzler.)
- No reading of absolute distance on a UMAP or MDS map. Proximity means "check for
  overlap", not a measured gap. (LDAvis.)
- No silent drop of themes under `min_support`. Show them in grey. (NN/g.)
- No equal weight for a wish and a workaround. Spent money and effort is stronger evidence
  than stated interest. (Mom Test.)
- No 3-D scatter, no dual y-axes, no pie chart of 800 themes.

## 6. Build order

1. 3.1 sub-score glyph and 3.2 trust strip: render-only plus one aggregate endpoint.
2. 3.3 sparklines and 3.7 verbatim drawer: small store changes.
3. 3.5 quadrant replaces the theme map.
4. 3.6 neighbourhood map and 3.9 icicle: stage 5 changes, then the UI.
5. 3.4 composition and 3.8 persona matrix: need verify on and better persona labels.
6. 3.10 idea strip: after the store-app name check lands in `pipeline/reviews.py`.

## 7. Verification notes

The research agent fetched 15+ pages. Two fetches failed on TLS (`hint.fm`,
`vis.stanford.edu`); those entries rest on search summaries and mirrors. The GummySearch
shutdown date, Kraftful's feature list after the Amplitude acquisition, and the exact UI
wording of Chattermill and Viable are from snippets only.
