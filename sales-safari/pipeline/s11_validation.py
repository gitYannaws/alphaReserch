"""Stage 11: one falsifiable kill-test per idea."""


def _plan_for(idea: dict) -> dict:
    return {
        "kill_test": (
            f"Offer a concierge prototype for '{idea['title']}' to 10 people from the "
            "same community and ask them to bring one real workflow case."
        ),
        "metric": "Qualified users who complete the workflow and ask for repeat access",
        "threshold": "Kill if fewer than 3 of 10 complete it or fewer than 2 ask to use it again",
        "timeframe": "7 days",
        "channel": "Forum replies, DMs where permitted, and linked evidence threads",
    }


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
