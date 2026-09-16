"""Ground-truth specification for the synthetic end-to-end benchmark.

WHY THIS EXISTS
---------------
`bench/score_gold.py` measures stage 3 only. Stages 4-12 (embed, cluster, score, filter,
rank, ideate) have no ground truth at all, because you cannot hand-label "which SaaS ideas
are latent in 64,822 documents" - that is an emergent property of a whole corpus, not a
per-document label. Run 329746779047 makes the gap concrete: 1,817 clusters in, 14 out,
4 ideas, and nothing tells you whether the 1,803 dropped clusters were noise correctly
rejected or signal wrongly discarded.

A synthetic corpus fixes that by construction. Plant K themes at KNOWN author/thread/post
counts, bury them in known distractors, run the real pipeline, and every stage becomes
measurable: recall against planted truth, precision against planted noise.

FOUR DESIGN RULES, each earned from a real failure mode in this repo
--------------------------------------------------------------------
1. **The generator must not be the extractor's model.** These templates are deterministic
   string assembly, not LLM output. If an LLM wrote the corpus and the same LLM extracted
   from it, the score measures self-consistency, not capability.

2. **Distractors must reproduce the observed false-positive classes.** The 2026-07-16 gold
   set found the extractor flagging jokes ("I'm 3 martinis down"), snark, and advice given
   to other people as market pain. A synthetic corpus without those classes would report a
   precision the real pipeline does not have. See NOISE_TEMPLATES.

3. **Sweep the variables the pipeline thresholds on.** Run 329746779047 dropped clusters
   for insufficient_evidence (1,775), insufficient_authors (1,538) and insufficient_threads
   (864). So authors/threads/posts per theme are the independent variables, and several
   themes are planted deliberately BELOW threshold. `expect_survives` records which.

4. **Realism gap is a known limitation, not a hidden one.** Synthetic prose is cleaner than
   forum prose, so extraction recall here is an UPPER bound. Use `--hybrid` to inject
   planted threads into real corpus text and get the pessimistic bound. Report both.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Theme:
    id: str
    label: str               # canonical name, for reporting only - never shown to the pipeline
    persona: str
    saas_shaped: bool        # is a software product a plausible answer? drives the ideate check
    complaints: tuple        # paraphrases of the SAME pain; one per post
    n_authors: int
    n_threads: int
    expect_survives: bool    # given the configured thresholds, should this reach ranked output?
    note: str = ""


# Nine planted themes. Five are comfortably above threshold, four sit at or below it on
# purpose - a harness where everything survives measures nothing about the filters.
THEMES: tuple = (
    Theme(
        id="T01", label="Recurring invoice chasing eats a day a week", persona="freelancer",
        saas_shaped=True, n_authors=9, n_threads=4, expect_survives=True,
        complaints=(
            "I spend most of Friday chasing invoices that are already two weeks late and it kills my billable time",
            "chasing late payments manually every single week is the worst part of running solo, I lose a full day to it",
            "my whole Friday goes to writing polite payment reminders one at a time, there has to be a better way",
            "tracking which clients have paid and which are overdue in a spreadsheet is eating hours I could bill",
            "I lose about a day a week to invoice follow-up emails and I still miss the ones that quietly go unpaid",
            "reconciling who paid against my invoice list by hand takes half a day and I always find one I missed",
        ),
    ),
    Theme(
        id="T02", label="Shift swap coordination happens in chat and gets lost", persona="restaurant manager",
        saas_shaped=True, n_authors=8, n_threads=3, expect_survives=True,
        complaints=(
            "every shift swap goes through a group chat and someone always misses it, then nobody shows up",
            "I approve shift swaps over text and then forget to update the rota, we were short two people last Saturday",
            "staff arrange cover between themselves in chat and I only find out when the wrong person turns up",
            "our rota lives in a spreadsheet but swaps happen in WhatsApp so the two never match",
            "I have to scroll back through days of messages to work out who agreed to cover which shift",
        ),
    ),
    Theme(
        id="T03", label="Onboarding docs go stale and nobody notices", persona="engineering lead",
        saas_shaped=True, n_authors=7, n_threads=3, expect_survives=True,
        complaints=(
            "our onboarding doc is six months out of date and every new hire hits the same broken setup step",
            "new starters follow the setup guide, it fails, and they lose two days before someone tells them the doc is wrong",
            "nobody owns the onboarding wiki so it rots, and I only learn it rotted when a new hire is blocked",
            "I rewrite the same three onboarding steps every quarter because the doc silently drifts from reality",
            "every new engineer wastes their first week on setup instructions that stopped working months ago",
        ),
    ),
    Theme(
        id="T04", label="Client asset handoff scattered across email threads", persona="agency owner",
        saas_shaped=True, n_authors=6, n_threads=3, expect_survives=True,
        complaints=(
            "client logos and brand files arrive over email and I can never find the latest version when I need it",
            "I have four versions of the same client logo in different email threads and no idea which is current",
            "chasing clients for their brand assets over email takes longer than the actual design work",
            "every project starts with me digging through six months of email to find the right source files",
            "asset handoff happens over email attachments so nothing is versioned and the newest one is a guess",
        ),
    ),
    Theme(
        id="T05", label="Compliance evidence collection is manual before every audit", persona="ops manager",
        saas_shaped=True, n_authors=6, n_threads=3, expect_survives=True,
        complaints=(
            "before every audit I screenshot the same twenty settings by hand and it takes a full week",
            "collecting compliance evidence means manually exporting logs from five systems every quarter",
            "I rebuild the same audit evidence pack from scratch each year because nothing captured it as we went",
            "audit prep is two weeks of screenshotting dashboards, and half of it is already stale by review day",
            "every auditor wants proof of the same controls and I gather it by hand from scratch each time",
        ),
    ),
    # ---- planted below threshold on purpose ----
    Theme(
        id="T06", label="Warehouse barcode scanner drops connection", persona="warehouse supervisor",
        saas_shaped=False, n_authors=2, n_threads=1, expect_survives=False,
        note="physical hardware pain; also below author threshold. Should be dropped twice over.",
        complaints=(
            "the handheld scanners lose wifi at the back of the warehouse and we lose the whole pick list",
            "our barcode guns disconnect constantly in the cold aisle and the count has to start again",
        ),
    ),
    Theme(
        id="T07", label="Single loud user complaining about pricing", persona="small business owner",
        saas_shaped=True, n_authors=1, n_threads=1, expect_survives=False,
        note="one author, many posts. The distinct-author guard exists precisely for this.",
        complaints=(
            "the pricing on these tools has got completely out of hand for a business my size",
            "another month another price rise, I am being priced out of software I depend on",
            "every vendor has moved to per-seat pricing and it is killing small teams like mine",
            "I cannot keep absorbing these annual increases, the pricing model is broken for small business",
        ),
    ),
    Theme(
        id="T08", label="Expense receipts lost between app and accountant", persona="consultant",
        saas_shaped=True, n_authors=3, n_threads=2, expect_survives=False,
        note="borderline: just under the author threshold. The most informative row in the table.",
        complaints=(
            "receipts photographed in one app never reach my accountant and I redo them at year end",
            "my expense app and my accountant's system do not talk so I enter everything twice",
            "half my receipts go missing between the phone app and the bookkeeping export",
        ),
    ),
    Theme(
        id="T09", label="Visa paperwork timing across two countries", persona="expat worker",
        saas_shaped=False, n_authors=7, n_threads=3, expect_survives=False,
        note="well above the author threshold but domain_policy should reject it as visa_immigration_process.",
        complaints=(
            "the visa paperwork has to be stamped in one country before the other will accept it and the windows never line up",
            "I flew back twice because the residence permit had to be collected in person within a fixed window",
            "coordinating visa appointments across two consulates wasted three months and two flights",
            "every immigration office gives different advice on the order the documents must be filed in",
            "my permit expired while the renewal sat in a queue and nobody could tell me where it was",
        ),
    ),
)

# Distractors. Every class here is a false positive the gold set actually caught the
# extractor making, plus plain off-topic chatter. A corpus without these overstates precision.
NOISE_TEMPLATES: dict = {
    "joke": (
        "I'm three martinis down so take this with salt but honestly the whole thing is fine",
        "at this point my keyboard has more coffee in it than my mug, send help lol",
        "my code compiles first try and I assume something has gone deeply wrong",
    ),
    "advice_to_others": (
        "you should just batch those and do them once a week, works fine for me",
        "honestly if it bothers you that much you should automate it, plenty of tools do this",
        "my advice would be to set a recurring reminder, that solved it for me years ago",
    ),
    "opinion_snark": (
        "hot take: most people complaining about this simply have not read the manual",
        "this whole thread is people inventing problems that do not exist",
        "unpopular opinion but the old way was better and everyone knows it",
    ),
    "offtopic": (
        "anyone watching the game tonight, the away form has been dreadful",
        "finally got the garden done this weekend, took three trips to the tip",
        "recommend me a decent laptop bag that survives daily commuting",
    ),
    "resolved": (
        "used to be a pain but the last update fixed it completely, no complaints now",
        "this annoyed me for years and then they shipped the feature, all good since",
    ),
}

# ---------------------------------------------------------------------------
# Round 2, added after run dd952fff8d8f. Clean-room round 1 scored 100% recall /
# 94.5% precision, so the easy classes stopped teaching anything. These target the
# three things that actually leaked or went untested:
#   T10  past-tense resolved pain      -- the ONLY distractor class that beat the
#                                         union (3 of 41 `resolved` docs flagged),
#                                         and it beat stage 3b, whose whole job is
#                                         type-classification.
#   T11/T12  two semantically adjacent themes -- round 1 never tested whether the
#                                         clusterer SPLITS related pains or merges
#                                         them. T06 hinted at the risk by landing in
#                                         a cluster at 20% purity.
#   T13  sarcasm carrying a real pain   -- inverse of the joke distractor: the text
#                                         reads like snark but states a genuine
#                                         workflow problem.
# ---------------------------------------------------------------------------
THEMES_ROUND2: tuple = (
    Theme(
        id="T10", label="Resolved: export bug fixed two releases ago", persona="analyst",
        saas_shaped=False, n_authors=7, n_threads=3, expect_survives=False,
        note="Past tense. The pain is over. Flagging it is a false market signal.",
        complaints=(
            "the CSV export used to mangle unicode every time but they fixed it two releases ago and it has been flawless since",
            "we had constant export failures last year, the vendor shipped a fix and I have not seen one since",
            "this used to eat a morning a week until the update landed, now it just works",
            "the duplicate-row bug annoyed me for ages and then it quietly got fixed, no complaints now",
            "exports were broken for months, that is all behind us since the migration",
            "I used to dread the monthly export, the new version handles it fine",
            "that was a real problem in the old release and it is completely gone now",
        ),
    ),
    Theme(
        id="T11", label="Contract renewal dates tracked in a spreadsheet", persona="office manager",
        saas_shaped=True, n_authors=7, n_threads=3, expect_survives=True,
        note="Deliberately adjacent to T12. If the clusterer merges these, purity drops on both.",
        complaints=(
            "I track every supplier contract renewal date in a spreadsheet and we auto-renewed one we meant to cancel",
            "our renewal dates live in a sheet nobody owns, so a contract rolled over before anyone looked",
            "I set calendar reminders by hand for each contract renewal and I still miss them",
            "we found out a licence had auto-renewed only when the invoice arrived",
            "keeping supplier renewal dates current in a spreadsheet is a job nobody wants",
            "nobody knows which contracts renew next quarter without opening the sheet",
            "we missed a cancellation window because the renewal date was only in one person's spreadsheet",
        ),
    ),
    Theme(
        id="T12", label="Software licence seat counts drift from actual headcount", persona="IT admin",
        saas_shaped=True, n_authors=7, n_threads=3, expect_survives=True,
        note="Adjacent to T11 (both procurement/licensing) but a distinct pain: seats, not dates.",
        complaints=(
            "we pay for forty seats and about twelve belong to people who left last year",
            "nobody reconciles licence seats against the staff list so we over-buy every renewal",
            "I audit seat counts by hand once a year and always find leavers still assigned",
            "our seat count and our headcount have not matched in two years",
            "we discovered we were paying for fifteen unused seats across three tools",
            "reclaiming licences when someone leaves depends on somebody remembering to do it",
            "seat allocation drifts constantly and the only check is my annual manual audit",
        ),
    ),
    Theme(
        id="T13", label="Sarcastic phrasing over a genuine deploy-rollback pain", persona="devops engineer",
        saas_shaped=True, n_authors=7, n_threads=3, expect_survives=True,
        note="Reads like snark; states a real workflow problem. Inverse of the joke distractor.",
        complaints=(
            "oh brilliant, another deploy where rolling back means manually reverting six services in the right order, love that for me",
            "nothing says Friday like a rollback runbook that is nine manual steps and one of them is always wrong",
            "sure, rollbacks are easy, if you enjoy sshing into four boxes and hoping you got the order right",
            "my favourite hobby is discovering the rollback procedure is out of date mid-incident",
            "great, the rollback doc references a service we renamed last quarter, cool cool cool",
            "truly nothing beats a manual rollback at 2am with no script and a stale runbook",
            "wonderful, another incident where undoing the deploy takes longer than the deploy",
        ),
    ),
)
