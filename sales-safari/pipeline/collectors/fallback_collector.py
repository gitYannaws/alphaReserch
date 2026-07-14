"""Collector wrapper that tries one open-source collector before another."""
from typing import Iterator

from .base import Collector, Document


class FallbackCollector(Collector):
    def __init__(self, primary: Collector, secondary: Collector,
                 primary_name: str = "primary", secondary_name: str = "secondary"):
        self.primary = primary
        self.secondary = secondary
        self.primary_name = primary_name
        self.secondary_name = secondary_name
        self.source_type = primary.source_type
        self.granularity = getattr(primary, "granularity", "post")

    def collect(self, seed_url: str, limit: int) -> Iterator[Document]:
        yielded = 0
        try:
            for doc in self.primary.collect(seed_url, limit):
                yielded += 1
                yield doc
        except Exception as e:
            print(f"  {self.primary_name} fallback triggered: {e}")

        if yielded:
            return

        print(f"  trying {self.secondary_name} fallback")
        yield from self.secondary.collect(seed_url, limit)
