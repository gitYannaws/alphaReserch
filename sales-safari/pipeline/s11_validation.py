"""Stage 11: one falsifiable kill-test per idea."""


def _kind_for(idea: dict) -> str:
    title = (idea.get("title") or "").lower()
    if title.startswith("tripfit brief"):
        return "destination"
    if title.startswith("streetwise date ledger"):
        return "safety"
    if title.startswith("match market audit"):
        return "app_quality"
    if title.startswith("expectation compass"):
        return "expectations"
    if title.startswith("lifestyle match filter"):
        return "lifestyle"
    return "research"


def _plan_for(idea: dict) -> dict:
    title = idea["title"]
    kind = _kind_for(idea)
    plans = {
        "destination": {
            "kill_test": (
                f"Manually produce five '{title}' destination briefs for people choosing "
                "between 2-4 countries or cities; each brief must rank options and name "
                "the tradeoff that changed the recommendation."
            ),
            "metric": "Travelers who use the brief to eliminate at least one destination or ask for a second brief",
            "threshold": "Kill if fewer than 3 of 5 say it changed their shortlist or fewer than 2 request another brief",
            "timeframe": "7 days",
            "channel": "Relevant Reddit threads, Discord/Telegram travel groups, and DMs where permitted",
        },
        "safety": {
            "kill_test": (
                f"Create a one-page '{title}' city risk memo from recent community stories "
                "and ask travelers to use it before a date, meetup, or nightlife plan."
            ),
            "metric": "Travelers who identify a concrete plan change, avoided venue/app pattern, or missing safety question",
            "threshold": "Kill if fewer than 4 of 10 report a specific decision change or saved checklist item",
            "timeframe": "10 days",
            "channel": "Incident-heavy evidence threads, safety comments, and private replies where permitted",
        },
        "app_quality": {
            "kill_test": (
                f"Publish three manual '{title}' scorecards comparing dating apps by city, "
                "fake-profile signs, response quality, bugs, and recent user complaints."
            ),
            "metric": "Users who compare the scorecards before choosing an app/city and request an updated scorecard",
            "threshold": "Kill if fewer than 25% of viewers click through or fewer than 3 users request updates",
            "timeframe": "14 days",
            "channel": "Dating-app complaint threads, city-specific posts, and a simple landing page",
        },
        "expectations": {
            "kill_test": (
                f"Run ten '{title}' expectation checks where users answer scenario questions "
                "about money, commitment, gender roles, and communication norms before a trip."
            ),
            "metric": "Users who uncover a mismatch they had not considered and want the checklist for a future destination",
            "threshold": "Kill if fewer than 4 of 10 name a new mismatch or fewer than 2 ask to reuse it",
            "timeframe": "10 days",
            "channel": "Long-form discussion threads, newsletter-style signup, and interviews from interested commenters",
        },
        "lifestyle": {
            "kill_test": (
                f"Offer a '{title}' screener to ten travelers comparing destinations by gym access, "
                "diet fit, nightlife pace, social routine, and maintenance effort."
            ),
            "metric": "Travelers who change their destination/channel choice or ask for a reusable filter",
            "threshold": "Kill if fewer than 3 of 10 change a choice or fewer than 2 request the filter template",
            "timeframe": "7 days",
            "channel": "Fitness/lifestyle comments inside travel dating communities and related groups",
        },
        "research": {
            "kill_test": (
                f"Build a cited '{title}' evidence board for one recurring complaint and ask "
                "users to vote on the most costly missing answer."
            ),
            "metric": "Users who add their own evidence, vote on a decision card, or ask for the next board",
            "threshold": "Kill if fewer than 5 users interact or fewer than 2 contribute new evidence",
            "timeframe": "7 days",
            "channel": "Evidence threads, forum replies, and a lightweight public board",
        },
    }
    return plans[kind]


def validation_run(store, run_id: str, progress=None) -> dict:
    ideas = store.get_ideas(run_id)
    if progress:
        progress(0, len(ideas))
    for i, idea in enumerate(ideas, start=1):
        store.save_validation_plan(run_id, idea["id"], _plan_for(idea))
        if progress:
            progress(i, len(ideas))
    store.set_stage(run_id, 11, "validation_planned")
    return {"plans": len(ideas)}
