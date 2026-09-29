"""Throttled progress logging with ETA."""

import logging
import time

log = logging.getLogger(__name__)


class Progress:
    def __init__(self, label: str, total: int, every: float = 15.0):
        self.label = label
        self.total = max(total, 0)
        self.done = 0
        self.every = every
        self.start = time.monotonic()
        self._last = 0.0

    def advance(self, n: int = 1, detail: str = "") -> None:
        self.done += n
        now = time.monotonic()
        if now - self._last >= self.every or self.done >= self.total:
            self._last = now
            log.info("%s: %s%s", self.label, self.summary(now), f" — {detail}" if detail else "")

    def summary(self, now: float | None = None) -> str:
        elapsed = (now or time.monotonic()) - self.start
        text = f"{self.done:,}/{self.total:,}"
        if self.total:
            text += f" ({100 * self.done / self.total:.1f}%)"
        if self.done and self.total > self.done:
            eta = elapsed * (self.total - self.done) / self.done
            text += f", ETA {_duration(eta)}"
        return text


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
