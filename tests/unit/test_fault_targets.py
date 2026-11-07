"""A controller fault narrowed to one I2C address or SPI chip select must leave the other targets alone."""

from __future__ import annotations

import struct

import pytest

from patchbay.chips.bno055 import Bno055
from patchbay.chips.sht3x import Sht3x
from patchbay.clock import FakeClock
from patchbay.faults import Fault, FaultPlan
from patchbay.i2c_bus import I2cBus
from patchbay.spi_bus import SpiBus
from patchbay.virtio_i2c import FLAGS_FAIL_NEXT, FLAGS_M_RD, MSG_ERR, MSG_OK, VirtioI2c
from patchbay.virtio_spi import HEAD, TRANS_ERR, TRANS_OK, VirtioSpi
from tests.ringkit import Arena, DriverRing


def hdr(addr: int, flags: int = 0) -> bytes:
    return struct.pack("<HHI", addr << 1, 0, flags)


def i2c_rig() -> tuple[FakeClock, FaultPlan, VirtioI2c, Arena, DriverRing]:
    clock = FakeClock(50.0)
    plan = FaultPlan()
    bus = I2cBus(plan)
    bus.attach(Bno055(clock))  # 0x28
    bus.attach(Sht3x(clock))  # 0x44
    a = Arena()
    return clock, plan, VirtioI2c(bus, plan, clock), a, DriverRing(a, size=8)


def controller_fault(kind: str, bus: str, target: int) -> Fault:
    return Fault(kind, bus, target=target, delay_ms=300 if kind == "delay" else 0)  # type: ignore[arg-type]


def test_a_fault_with_a_target_does_not_match_a_request_that_names_no_target() -> None:
    plan = FaultPlan()
    plan.add(Fault("error", "i2c", target=0x28))
    assert plan.take("i2c", "error") is None
    assert plan.faults[0].count is None and len(plan.faults) == 1


def test_a_fault_without_a_target_matches_every_target() -> None:
    plan = FaultPlan()
    plan.add(Fault("error", "i2c"))
    assert plan.active("i2c", "error", 0x44) is not None
    assert plan.active("i2c", "error") is not None


@pytest.mark.parametrize("kind", ["error", "delay", "stuck"])
def test_an_i2c_controller_fault_aimed_at_0x28_leaves_a_request_to_0x44_untouched(kind: str) -> None:
    _, plan, dev, a, d = i2c_rig()
    plan.add(controller_fault(kind, "i2c", 0x28))
    _, ins = d.add([hdr(0x44)], [1])  # a zero-length write: the SHT3x acknowledges its address
    done = dev.kick(d.device())
    assert len(done) == 1 and a.read(ins[0], 1) == bytes([MSG_OK])
    assert len(plan.faults) == 1  # not used up


@pytest.mark.parametrize("kind", ["error", "delay", "stuck"])
def test_an_i2c_controller_fault_aimed_at_0x28_still_hits_a_request_to_0x28(kind: str) -> None:
    _, plan, dev, a, d = i2c_rig()
    plan.add(controller_fault(kind, "i2c", 0x28))
    d.add([hdr(0x28, FLAGS_FAIL_NEXT), b"\x00"], [1])
    _, read = d.add([hdr(0x28, FLAGS_M_RD)], [2])
    done = dev.kick(d.device())
    if kind == "error":
        assert len(done) == 2 and a.read(read[0], 2)[-1] == MSG_ERR
    else:
        assert done == []  # held back: delayed, or stuck


def test_a_group_that_reaches_0x28_and_0x44_is_hit_by_a_fault_aimed_at_0x28() -> None:
    _, plan, dev, a, d = i2c_rig()
    plan.add(controller_fault("error", "i2c", 0x28))
    _, first = d.add([hdr(0x44, FLAGS_FAIL_NEXT)], [1])
    _, second = d.add([hdr(0x28)], [1])
    dev.kick(d.device())
    assert a.read(first[0], 1) == bytes([MSG_ERR]) and a.read(second[0], 1) == bytes([MSG_ERR])


def test_a_delay_aimed_at_0x28_does_not_hold_up_0x44_but_holds_0x28_for_its_delay() -> None:
    clock, plan, dev, a, d = i2c_rig()
    plan.add(controller_fault("delay", "i2c", 0x28))
    d.add([hdr(0x28)], [1])
    assert dev.kick(d.device()) == []
    assert dev.next_deadline() == pytest.approx(clock.now() + 0.3)
    clock.advance(0.301)
    assert len(dev.poll()) == 1


def spi_rig() -> tuple[FaultPlan, VirtioSpi, Arena, DriverRing]:
    class Echo:
        name = "echo"

        def exchange(self, tx: bytes) -> bytes:
            return bytes(b ^ 0xFF for b in tx)

    plan = FaultPlan()
    bus = SpiBus(cs_count=2, faults=plan)
    bus.attach(0, Echo())
    bus.attach(1, Echo())
    a = Arena()
    return plan, VirtioSpi(bus, plan, FakeClock()), a, DriverRing(a)


def spi_head(cs: int) -> bytes:
    return HEAD.pack(cs, 8, 0, 1, 1, 0, 1_000_000, 0, 0, 0, 0)


@pytest.mark.parametrize("kind", ["error", "delay", "stuck"])
def test_an_spi_controller_fault_aimed_at_chip_select_0_leaves_chip_select_1_untouched(kind: str) -> None:
    plan, dev, a, d = spi_rig()
    plan.add(controller_fault(kind, "spi", 0))
    _, ins = d.add([spi_head(1), b"\x00"], [2])
    done = dev.kick(d.device())
    assert len(done) == 1 and a.read(ins[0], 2)[-1] == TRANS_OK
    assert len(plan.faults) == 1


@pytest.mark.parametrize("kind", ["error", "delay", "stuck"])
def test_an_spi_controller_fault_aimed_at_chip_select_0_still_hits_chip_select_0(kind: str) -> None:
    plan, dev, a, d = spi_rig()
    plan.add(controller_fault(kind, "spi", 0))
    _, ins = d.add([spi_head(0), b"\x00"], [2])
    done = dev.kick(d.device())
    if kind == "error":
        assert len(done) == 1 and a.read(ins[0], 2)[-1] == TRANS_ERR
    else:
        assert done == []
