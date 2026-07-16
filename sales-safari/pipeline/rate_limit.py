"""Shared adaptive pacing for collectors with unknown site limits."""

import random
import time


class AdaptiveRateLimiter:
    """Back off on throttling signals, then gradually recover after successes."""

    def __init__(self, min_delay: float, max_delay: float, *,
                 backoff: float = 1.6, max_multiplier: float = 8.0,
                 recover: float = 0.9, retries: int = 2,
                 cooldown_seconds: float = 5.0):
        self.min_delay = float(min_delay)
        self.max_delay = float(max_delay)
        self.backoff = float(backoff)
        self.max_multiplier = float(max_multiplier)
        self.recover = float(recover)
        self.retries = max(0, int(retries))
        self.cooldown_seconds = float(cooldown_seconds)
        self.hits = 0
        self.multiplier = 1.0

    def sleep(self):
        time.sleep(random.uniform(self.min_delay, self.max_delay) * self.multiplier)

    def hit(self, attempt: int) -> bool:
        self.hits += 1
        self.multiplier = min(self.multiplier * self.backoff, self.max_multiplier)
        if attempt < self.retries:
            time.sleep(self.cooldown_seconds * self.multiplier)
            return True
        return False

    def success(self):
        self.multiplier = max(1.0, self.multiplier * self.recover)
