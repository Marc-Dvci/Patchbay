"""The fault plan, and how the I2C and SPI buses apply wire-level faults."""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patchbay.faults import Fault, FaultPlan, flip_one_bit
from patchbay.i2c_bus import I2cBus, I2cChip, Segment
from patchbay.spi_bus import MODE_LOOP, MODE_LSB_FIRST, SpiBus, SpiTransfer, reverse_bits


class Echo(I2cChip):
    def __init__(self, address: int) -> None:
        self.address = address
        self.stops = 0
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> bool:
        self.writes.append(data)
        return True

    def read(self, n: int) -> bytes:
        return bytes([self.address] * n)

    def stop(self) -> None:
        self.stops += 1


class Fixed:
    name = "fixed"

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.seen: list[bytes] = []

    def exchange(self, tx: bytes) -> bytes:
        self.seen.append(tx)
        return self.reply[: len(tx)]


def bus_with(*addrs: int) -> tuple[I2cBus, FaultPlan]:
    plan = FaultPlan()
    bus = I2cBus(plan)
    for a in addrs:
        bus.attach(Echo(a))
    return bus, plan


def test_a_nak_fault_defaults_to_one_transfer_and_then_goes_away() -> None:
    bus, plan = bus_with(0x28)
    plan.add(Fault("nak", "i2c", target=0x28))
    assert not bus.transfer([Segment(0x28, True, length=1)])[0].ok
    assert bus.transfer([Segment(0x28, True, length=1)])[0].ok
    assert plan.faults == []


def test_a_fault_narrowed_to_one_address_leaves_the_others_alone() -> None:
    bus, plan = bus_with(0x28, 0x44)
    plan.add(Fault("unplug", "i2c", target=0x28))
    assert not bus.transfer([Segment(0x28, True, length=1)])[0].ok
    assert bus.transfer([Segment(0x44, True, length=1)])[0].ok
    assert plan.faults  # unplug lasts until cleared


def test_an_absent_address_is_not_acknowledged() -> None:
    bus, _ = bus_with(0x28)
    assert not bus.transfer([Segment(0x50, False, b"\x00")])[0].ok


def test_after_a_failed_segment_the_rest_of_the_transaction_never_reaches_the_bus() -> None:
    bus, plan = bus_with(0x28, 0x44)
    chip44 = bus.chips[0x44]
    assert isinstance(chip44, Echo)
    plan.add(Fault("nak", "i2c", target=0x28))
    res = bus.transfer([Segment(0x28, False, b"\x01"), Segment(0x44, False, b"\x02"), Segment(0x44, True, length=2)])
    assert [r.ok for r in res] == [False, False, False]
    assert chip44.writes == []


def test_a_transaction_ends_with_one_stop_per_chip_touched() -> None:
    bus, _ = bus_with(0x28)
    chip = bus.chips[0x28]
    assert isinstance(chip, Echo)
    bus.transfer([Segment(0x28, False, b"\x1a"), Segment(0x28, True, length=6)])
    assert chip.stops == 1


def test_corrupt_flips_exactly_one_bit_and_is_repeatable_with_its_seed() -> None:
    outs = []
    for _ in range(2):
        bus, plan = bus_with(0x28)
        plan.add(Fault("corrupt", "i2c", target=0x28, count=1, seed=7))
        outs.append(bus.transfer([Segment(0x28, True, length=6)])[0].data)
    clean = bytes([0x28] * 6)
    assert outs[0] == outs[1]
    assert sum(bin(a ^ b).count("1") for a, b in zip(outs[0], clean, strict=True)) == 1


@given(data=st.binary(min_size=1, max_size=32), seed=st.integers(0, 2**32))
def test_flip_one_bit_changes_one_bit_of_any_input(data: bytes, seed: int) -> None:
    out = flip_one_bit(data, random.Random(seed))
    assert len(out) == len(data)
    assert sum(bin(a ^ b).count("1") for a, b in zip(out, data, strict=True)) == 1


def test_clear_removes_by_bus_or_all() -> None:
    plan = FaultPlan()
    plan.add(Fault("stuck", "i2c"))
    plan.add(Fault("stuck", "spi"))
    assert plan.clear("i2c") == 1 and [f.bus for f in plan.faults] == ["spi"]
    assert plan.clear() == 1 and plan.faults == []


@pytest.mark.parametrize(
    "kw",
    [
        {"kind": "melt", "bus": "i2c"},
        {"kind": "nak", "bus": "can"},
        {"kind": "nak", "bus": "spi"},
        {"kind": "delay", "bus": "i2c"},
        {"kind": "stuck", "bus": "i2c", "count": 0},
    ],
)
def test_faults_that_make_no_sense_are_refused(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Fault(**kw)  # type: ignore[arg-type]


def test_spi_unplug_reads_zeros_and_other_chip_selects_still_answer() -> None:
    plan = FaultPlan()
    bus = SpiBus(cs_count=2, faults=plan)
    bus.attach(0, Fixed(b"\xaa\xbb\xcc"))
    bus.attach(1, Fixed(b"\x11\x22\x33"))
    plan.add(Fault("unplug", "spi", target=0))
    assert bus.transfer(SpiTransfer(0, b"\x01\x02\x03", 3)) == b"\x00\x00\x00"
    assert bus.transfer(SpiTransfer(1, b"\x01\x02\x03", 3)) == b"\x11\x22\x33"


def test_spi_lsb_first_reverses_bits_on_the_wire_both_ways() -> None:
    bus = SpiBus()
    chip = Fixed(bytes([reverse_bits(0x01)]))
    bus.attach(0, chip)
    assert bus.transfer(SpiTransfer(0, b"\x80", 1, mode=MODE_LSB_FIRST)) == b"\x01"
    assert chip.seen == [b"\x01"]


def test_spi_loopback_returns_what_was_sent_without_a_chip() -> None:
    bus = SpiBus()
    assert bus.transfer(SpiTransfer(1, b"\xde\xad", 2, mode=MODE_LOOP)) == b"\xde\xad"


def test_spi_read_only_transfers_clock_out_zeros() -> None:
    bus = SpiBus()
    chip = Fixed(b"\x55\x66")
    bus.attach(0, chip)
    assert bus.transfer(SpiTransfer(0, b"", 2)) == b"\x55\x66"
    assert chip.seen == [b"\x00\x00"]


def test_spi_chip_selects_are_checked_when_attaching() -> None:
    bus = SpiBus(cs_count=2)
    with pytest.raises(ValueError):
        bus.attach(2, Fixed(b""))
    bus.attach(0, Fixed(b""))
    with pytest.raises(ValueError):
        bus.attach(0, Fixed(b""))
