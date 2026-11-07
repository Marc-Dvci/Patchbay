"""Faults the back end injects, chosen at run time through the control socket.

  nak      the addressed I2C chip does not acknowledge its address, for `count` transfers (default 1)
  unplug   the chip is gone: an I2C address is never acknowledged, an SPI chip leaves MISO low
  corrupt  one bit flips in the data read from the chip, at a position drawn from a seeded generator
  stuck    the controller never completes a request until the fault is cleared or the driver resets the device
  delay    the controller completes each request `delay_ms` late
  error    the controller reports a transfer error (VIRTIO_I2C_MSG_ERR, VIRTIO_SPI_TRANS_ERR) without touching the bus

`target` narrows a fault to one I2C address or one SPI chip select; None means every target on that bus. A fault that
lives in the controller (stuck, delay, error) hits a request when the request touches the target: an I2C group that
reaches several addresses is hit if any of them is the target.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["nak", "unplug", "corrupt", "stuck", "delay", "error"]
Bus = Literal["i2c", "spi"]
KINDS: tuple[str, ...] = ("nak", "unplug", "corrupt", "stuck", "delay", "error")
# Faults that live in the controller rather than on the wire: they apply to the whole request, whatever its target.
CONTROLLER_KINDS = frozenset({"stuck", "delay", "error"})


@dataclass
class Fault:
    kind: Kind
    bus: Bus
    target: int | None = None
    count: int | None = None  # requests affected; None lasts until cleared
    delay_ms: int = 0
    seed: int = 0
    rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown fault kind {self.kind!r}")
        if self.bus not in ("i2c", "spi"):
            raise ValueError(f"unknown bus {self.bus!r}")
        if self.kind == "nak" and self.count is None:
            self.count = 1
        if self.count is not None and self.count < 1:
            raise ValueError("count must be at least 1")
        if self.kind == "delay" and self.delay_ms <= 0:
            raise ValueError("a delay fault needs delay_ms > 0")
        if self.kind == "nak" and self.bus == "spi":
            raise ValueError("SPI has no acknowledge: use unplug or error")
        self.rng = random.Random(self.seed)

    def matches(self, bus: str, target: int | None) -> bool:
        """A fault without a target hits every target on its bus. One with a target hits only that target, so a
        request that names no target is not hit by it."""
        return self.bus == bus and (self.target is None or self.target == target)

    def describe(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "bus": self.bus,
            "target": self.target,
            "count": self.count,
            "delay_ms": self.delay_ms,
            "seed": self.seed,
        }


class FaultPlan:
    def __init__(self) -> None:
        self.faults: list[Fault] = []

    def add(self, fault: Fault) -> None:
        self.faults.append(fault)

    def clear(self, bus: str | None = None) -> int:
        before = len(self.faults)
        self.faults = [f for f in self.faults if bus is not None and f.bus != bus]
        return before - len(self.faults)

    def active(self, bus: str, kind: str, target: int | None = None) -> Fault | None:
        for f in self.faults:
            if f.kind == kind and f.matches(bus, target):
                return f
        return None

    def take(self, bus: str, kind: str, target: int | None = None) -> Fault | None:
        """The first matching fault, with one use counted against it."""
        return self._use(self.active(bus, kind, target))

    def active_any(self, bus: str, kind: str, targets: Iterable[int]) -> Fault | None:
        """The first fault of this kind that hits at least one of the targets a request touches."""
        wanted = list(targets)
        for f in self.faults:
            if f.kind == kind and any(f.matches(bus, t) for t in wanted):
                return f
        return None

    def take_any(self, bus: str, kind: str, targets: Iterable[int]) -> Fault | None:
        return self._use(self.active_any(bus, kind, targets))

    def _use(self, f: Fault | None) -> Fault | None:
        if f is not None and f.count is not None:
            f.count -= 1
            if f.count == 0:
                self.faults.remove(f)
        return f

    def describe(self) -> list[dict[str, object]]:
        return [f.describe() for f in self.faults]


def flip_one_bit(data: bytes, rng: random.Random) -> bytes:
    """data with exactly one bit inverted. Empty data stays empty."""
    if not data:
        return data
    out = bytearray(data)
    i = rng.randrange(len(out))
    out[i] ^= 1 << rng.randrange(8)
    return bytes(out)
