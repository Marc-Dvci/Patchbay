"""An I2C bus with simulated chips on it, and the wire-level faults.

A transfer is a list of segments sent as one combined transaction: a start, then a repeated start before each further
segment, then one stop. That is how the virtio-i2c device runs a group of requests chained by
VIRTIO_I2C_FLAGS_FAIL_NEXT (virtio 1.2, 5.19.6). When a segment fails, the master stops, and every later segment in
the transaction fails without reaching the bus.
"""

from __future__ import annotations

from dataclasses import dataclass

from .faults import FaultPlan, flip_one_bit


@dataclass(frozen=True)
class Segment:
    addr: int  # 7-bit address
    read: bool
    data: bytes = b""  # bytes to write
    length: int = 0  # bytes to read


@dataclass(frozen=True)
class SegmentResult:
    ok: bool
    data: bytes = b""


class I2cChip:
    """A target on the bus. Subclasses model one part from its datasheet."""

    address: int = 0
    name: str = "chip"

    def acks_address(self, read: bool) -> bool:
        """Whether the chip acknowledges its address for this direction now."""
        return True

    def write(self, data: bytes) -> bool:
        """Bytes after the address byte. False means the chip did not acknowledge one of them."""
        return True

    def read(self, n: int) -> bytes:
        return bytes(n)

    def stop(self) -> None:
        """A stop condition ended the transaction."""


class I2cBus:
    def __init__(self, faults: FaultPlan | None = None) -> None:
        self.chips: dict[int, I2cChip] = {}
        self.faults = faults or FaultPlan()
        self.log: list[tuple[int, bool, int, bool]] = []  # (addr, read, length, ok), for tests and the trace

    def attach(self, chip: I2cChip) -> None:
        if not 0x08 <= chip.address <= 0x77:
            raise ValueError(f"{chip.address:#x} is a reserved 7-bit address")
        if chip.address in self.chips:
            raise ValueError(f"address {chip.address:#x} already taken")
        self.chips[chip.address] = chip

    def transfer(self, segments: list[Segment]) -> list[SegmentResult]:
        results: list[SegmentResult] = []
        failed = False
        touched: set[int] = set()
        for seg in segments:
            if failed:
                results.append(SegmentResult(ok=False))
                continue
            res = self._segment(seg)
            touched.add(seg.addr)
            self.log.append((seg.addr, seg.read, len(seg.data) if not seg.read else seg.length, res.ok))
            results.append(res)
            failed = not res.ok
        for addr in touched:
            chip = self.chips.get(addr)
            if chip is not None:
                chip.stop()
        return results

    def _segment(self, seg: Segment) -> SegmentResult:
        chip = self.chips.get(seg.addr)
        if chip is None or self.faults.active("i2c", "unplug", seg.addr):
            return SegmentResult(ok=False)
        if self.faults.take("i2c", "nak", seg.addr):
            return SegmentResult(ok=False)
        if not chip.acks_address(seg.read):
            return SegmentResult(ok=False)
        if not seg.read:
            return SegmentResult(ok=chip.write(seg.data))
        data = chip.read(seg.length)
        f = self.faults.take("i2c", "corrupt", seg.addr)
        if f is not None:
            data = flip_one_bit(data, f.rng)
        return SegmentResult(ok=True, data=data)
