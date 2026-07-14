"""Source-agnostic collector interface. Add a subclass per source (discourse, firecrawl, reddit...)."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Document:
    """One collected unit. For Discourse = a single post; for Firecrawl = a whole thread.

    Feeds §3 pain extraction. `author` carries through to §5 distinct-author counting.
    """
    source_type: str
    source_url: str          # unique permalink (post-level when available)
    permalink: str
    title: str               # thread title
    raw_markdown: str        # post text (or thread markdown for firecrawl)
    source_granularity: str = "post"
    author: Optional[str] = None
    thread_url: Optional[str] = None
    created_at: Optional[str] = None
    # Community endorsement (net upvotes/likes) when the source exposes it; None = source
    # has no vote signal. Optional so collectors stay source-agnostic; scoring degrades to 0.
    score: Optional[int] = None
    fetched_at: str = field(default_factory=_now)


class Collector:
    source_type = "base"
    # "post" = per-post authored (distinct-author signal usable); "thread" = whole-thread
    # blob without author attribution. Discovery uses this to pick its validation gate.
    granularity = "post"

    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        raise NotImplementedError
