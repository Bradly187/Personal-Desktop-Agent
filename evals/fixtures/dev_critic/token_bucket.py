"""Token-bucket rate limiter — fixture for dev_critic eval (dc-03)."""

import time


class TokenBucket:
    def __init__(self, rate, capacity):
        self.rate = rate          # tokens added per second
        self.capacity = capacity  # max tokens
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now
