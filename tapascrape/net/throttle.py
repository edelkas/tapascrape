"""Shared rate limiting and retry with exponential backoff."""

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableError(Exception):
    """A failure worth retrying (network error, 429, 5xx...)."""


class Throttle:
    """Ensures at most `rate` calls per second across every client sharing it."""

    def __init__(self, rate: float, jitter: float = 0.1):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.jitter = jitter
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval * (1 + random.uniform(0, self.jitter))

    def call(self, fn: Callable[[], T], max_retries: int = 5, what: str = "request") -> T:
        """Run `fn` after waiting for a slot, retrying RetryableError with backoff."""
        for attempt in range(max_retries + 1):
            self.wait()
            try:
                return fn()
            except RetryableError as e:
                if attempt == max_retries:
                    raise
                delay = min(300.0, 2.0 ** (attempt + 1)) + random.uniform(0, 1)
                log.warning("%s failed (%s); retry %d/%d in %.0fs",
                            what, e, attempt + 1, max_retries, delay)
                time.sleep(delay)
        raise AssertionError("unreachable")
