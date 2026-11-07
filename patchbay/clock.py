"""Time as the simulated chips see it. Tests pass a FakeClock; the server uses the host's monotonic clock."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FreezableClock:
    """The host's monotonic clock, which the control socket can stop and restart without a jump. While it is stopped
    the simulated motion and signals hold still, so a faulted reading can be compared with a fixed clean one."""

    def __init__(self) -> None:
        self.offset = 0.0
        self.frozen_at: float | None = None

    def now(self) -> float:
        if self.frozen_at is not None:
            return self.frozen_at
        return time.monotonic() - self.offset

    def freeze(self) -> None:
        if self.frozen_at is None:
            self.frozen_at = self.now()

    def thaw(self) -> None:
        if self.frozen_at is not None:
            self.offset = time.monotonic() - self.frozen_at
            self.frozen_at = None
