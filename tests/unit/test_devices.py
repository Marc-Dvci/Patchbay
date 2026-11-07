"""The virtio I2C and SPI devices: request parsing, groups, replies, and the controller-level faults."""

from __future__ import annotations

import struct

import pytest

from patchbay.chips.bno055 import Bno055
from patchbay.clock import FakeClock
from patchbay.faults import Fault, FaultPlan
from patchbay.i2c_bus import I2cBus
from patchbay.spi_bus import SpiBus
from patchbay.virtio_i2c import FLAGS_FAIL_NEXT, FLAGS_M_RD, MSG_ERR, MSG_OK, VirtioI2c
from patchbay.virtio_spi import HEAD, PARAM_ERR, TRANS_ERR, TRANS_OK, SpiConfig, VirtioSpi
from tests.ringkit import Arena, DriverRing


def i2c_hdr(addr: int, flags: int) -> bytes:
    return struct.pack("<HHI", addr << 1, 0, flags)


def i2c_rig() -> tuple[FakeClock, FaultPlan, VirtioI2c, Arena, DriverRing]:
    clock = FakeClock(50.0)
    plan = FaultPlan()
    bus = I2cBus(plan)
    bus.attach(Bno055(clock))
    a = Arena()
    return clock, plan, VirtioI2c(bus, plan, clock), a, DriverRing(a, size=8)


def register_read(d: DriverRing, reg: int, n: int, addr: int = 0x28) -> list[int]:
    """Write the register pointer, then read n bytes: two requests in one FAIL_NEXT group."""
    _, w = d.add([i2c_hdr(addr, FLAGS_FAIL_NEXT), bytes([reg])], [1])
    _, r = d.add([i2c_hdr(addr, FLAGS_M_RD)], [n + 1])
    return [w[0], r[0]]


def test_a_write_read_group_reads_the_chip_id_and_both_statuses_are_ok() -> None:
    _, _, dev, a, d = i2c_rig()
    w, r = register_read(d, 0x00, 1)
    done = dev.kick(d.device())
    assert [n for _, n in done] == [1, 2]
    assert a.read(w, 1) == bytes([MSG_OK])
    assert a.read(r, 2) == bytes([0xA0, MSG_OK])


def test_a_group_waits_for_its_last_request() -> None:
    _, _, dev, a, d = i2c_rig()
    ring = d.device()
    _, w = d.add([i2c_hdr(0x28, FLAGS_FAIL_NEXT), b"\x00"], [1])
    assert dev.kick(ring) == []
    _, r = d.add([i2c_hdr(0x28, FLAGS_M_RD)], [2])
    assert len(dev.kick(ring)) == 2


def test_a_nak_on_the_first_request_fails_the_whole_group() -> None:
    _, plan, dev, a, d = i2c_rig()
    plan.add(Fault("nak", "i2c", target=0x28))
    w, r = register_read(d, 0x00, 1)
    dev.kick(d.device())
    assert a.read(w, 1) == bytes([MSG_ERR])
    assert a.read(r, 2) == bytes([0, MSG_ERR])
    assert dev.failed == 1


def test_a_read_request_carrying_write_data_is_answered_with_an_error() -> None:
    _, _, dev, a, d = i2c_rig()
    _, ins = d.add([i2c_hdr(0x28, FLAGS_M_RD), b"\x00"], [2])
    dev.kick(d.device())
    assert a.read(ins[0], 2)[-1] == MSG_ERR


def test_a_header_shorter_than_eight_bytes_is_answered_with_an_error() -> None:
    _, _, dev, a, d = i2c_rig()
    _, ins = d.add([b"\x50\x00"], [1])
    dev.kick(d.device())
    assert a.read(ins[0], 1) == bytes([MSG_ERR])


def test_a_request_without_room_for_a_status_is_returned_with_nothing_written() -> None:
    _, _, dev, a, d = i2c_rig()
    head, _ = d.add([i2c_hdr(0x28, 0)], [])
    assert dev.kick(d.device()) == [(head, 0)]


def test_zero_length_write_probes_the_address() -> None:
    _, _, dev, a, d = i2c_rig()
    _, ok = d.add([i2c_hdr(0x28, 0)], [1])
    _, gone = d.add([i2c_hdr(0x50, 0)], [1])
    dev.kick(d.device())
    assert a.read(ok[0], 1) == bytes([MSG_OK]) and a.read(gone[0], 1) == bytes([MSG_ERR])


def test_stuck_holds_requests_until_cleared_and_reset_drops_them() -> None:
    _, plan, dev, a, d = i2c_rig()
    ring = d.device()
    plan.add(Fault("stuck", "i2c"))
    register_read(d, 0x00, 1)
    assert dev.kick(ring) == [] and dev.next_deadline() is None
    plan.clear()
    assert len(dev.poll()) == 2
    plan.add(Fault("stuck", "i2c"))
    register_read(d, 0x00, 1)
    dev.kick(ring)
    dev.reset()
    plan.clear()
    assert dev.poll() == []


def test_delay_completes_the_group_after_its_delay() -> None:
    clock, plan, dev, a, d = i2c_rig()
    plan.add(Fault("delay", "i2c", delay_ms=300, count=1))
    register_read(d, 0x00, 1)
    assert dev.kick(d.device()) == []
    assert dev.next_deadline() == pytest.approx(clock.now() + 0.3)
    clock.advance(0.299)
    assert dev.poll() == []
    clock.advance(0.002)
    assert len(dev.poll()) == 2


def test_error_fails_the_group_without_touching_the_bus() -> None:
    _, plan, dev, a, d = i2c_rig()
    plan.add(Fault("error", "i2c", count=1))
    w, r = register_read(d, 0x00, 1)
    dev.kick(d.device())
    assert a.read(r, 2)[-1] == MSG_ERR
    assert dev.bus.log == []


# --- SPI -------------------------------------------------------------------------------------------------------


class Echo:
    name = "echo"

    def exchange(self, tx: bytes) -> bytes:
        return bytes(b ^ 0xFF for b in tx)


def spi_head(cs: int = 0, bpw: int = 8, mode: int = 0, freq: int = 1_000_000) -> bytes:
    return HEAD.pack(cs, bpw, 0, 1, 1, mode, freq, 0, 0, 0, 0)


def spi_rig(cfg: SpiConfig | None = None) -> tuple[FaultPlan, VirtioSpi, Arena, DriverRing]:
    clock = FakeClock()
    plan = FaultPlan()
    bus = SpiBus(cs_count=2, faults=plan)
    bus.attach(0, Echo())
    a = Arena()
    return plan, VirtioSpi(bus, plan, clock, spi_config=cfg or SpiConfig()), a, DriverRing(a)


def test_spi_config_space_is_32_bytes_in_the_virtio_layout() -> None:
    raw = SpiConfig().pack()
    assert len(raw) == 32
    assert raw[0] == 2 and struct.unpack_from("<I", raw, 12)[0] == 50_000_000


def test_spi_full_duplex_returns_the_chips_bytes_and_ok() -> None:
    _, dev, a, d = spi_rig()
    _, ins = d.add([spi_head(), b"\x01\x80\x00"], [4])
    dev.kick(d.device())
    assert a.read(ins[0], 4) == bytes([0xFE, 0x7F, 0xFF, TRANS_OK])


@pytest.mark.parametrize(
    "head,tx,rx",
    [
        (spi_head(cs=2), b"\x00", 2),  # chip select beyond cs_max_number
        (spi_head(bpw=16), b"\x00\x00", 3),  # word width not in the mask
        (spi_head(freq=60_000_000), b"\x00", 2),  # over max_freq_hz
        (spi_head(mode=1 << 2), b"\x00", 2),  # CS_HIGH not offered
        (spi_head(), b"\x00\x00", 2),  # tx and rx lengths differ
        (b"\x00" * 10, b"", 2),  # head too short
    ],
)
def test_spi_parameters_the_controller_does_not_offer_get_param_err(head: bytes, tx: bytes, rx: int) -> None:
    _, dev, a, d = spi_rig()
    _, ins = d.add([head + tx], [rx])
    dev.kick(d.device())
    assert a.read(ins[0], rx)[-1] == PARAM_ERR


def test_spi_error_fault_reports_a_transfer_error() -> None:
    plan, dev, a, d = spi_rig()
    plan.add(Fault("error", "spi", target=0, count=1))
    _, ins = d.add([spi_head(), b"\x00"], [2])
    dev.kick(d.device())
    assert a.read(ins[0], 2)[-1] == TRANS_ERR


def test_spi_stuck_holds_the_transfer() -> None:
    plan, dev, a, d = spi_rig()
    plan.add(Fault("stuck", "spi"))
    d.add([spi_head(), b"\x00"], [2])
    assert dev.kick(d.device()) == []
    plan.clear()
    assert len(dev.poll()) == 1
