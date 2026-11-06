"""An SPI controller with simulated chips on its chip selects, and the wire-level faults.

One virtio-spi request is one transfer with chip select held for its length. SPI has no acknowledge, so a missing chip
cannot be detected on the wire: the master clocks in whatever MISO carries, which the model takes as 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .faults import FaultPlan, flip_one_bit

MODE_CPHA = 1 << 0
MODE_CPOL = 1 << 1
MODE_CS_HIGH = 1 << 2
MODE_LSB_FIRST = 1 << 3
MODE_LOOP = 1 << 4


class SpiChip(Protocol):
    name: str

    def exchange(self, tx: bytes) -> bytes: ...


def reverse_bits(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


@dataclass(frozen=True)
class SpiTransfer:
    cs: int
    tx: bytes
    rx_len: int
    mode: int = 0
    freq: int = 0
    bits_per_word: int = 8


class SpiBus:
    def __init__(self, cs_count: int = 2, faults: FaultPlan | None = None) -> None:
        self.cs_count = cs_count
        self.chips: dict[int, SpiChip] = {}
        self.faults = faults or FaultPlan()
        self.log: list[tuple[int, int, int]] = []  # (cs, tx bytes, rx bytes)

    def attach(self, cs: int, chip: SpiChip) -> None:
        if not 0 <= cs < self.cs_count:
            raise ValueError(f"chip select {cs} outside 0..{self.cs_count - 1}")
        if cs in self.chips:
            raise ValueError(f"chip select {cs} already taken")
        self.chips[cs] = chip

    def transfer(self, xfer: SpiTransfer) -> bytes:
        """MISO bytes for one transfer, rx_len long."""
        n = max(len(xfer.tx), xfer.rx_len)
        tx = xfer.tx + bytes(n - len(xfer.tx))
        self.log.append((xfer.cs, len(xfer.tx), xfer.rx_len))
        if xfer.mode & MODE_LOOP:
            return tx[: xfer.rx_len]
        chip = self.chips.get(xfer.cs)
        if chip is None or self.faults.active("spi", "unplug", xfer.cs):
            return bytes(xfer.rx_len)
        lsb = bool(xfer.mode & MODE_LSB_FIRST)
        wire = bytes(reverse_bits(b) for b in tx) if lsb else tx
        rx = chip.exchange(wire)
        if lsb:
            rx = bytes(reverse_bits(b) for b in rx)
        f = self.faults.take("spi", "corrupt", xfer.cs)
        if f is not None:
            rx = flip_one_bit(rx[: xfer.rx_len], f.rng)
        return rx[: xfer.rx_len]
