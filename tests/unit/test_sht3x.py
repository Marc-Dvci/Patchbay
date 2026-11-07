"""The SHT3x model against the Sensirion SHT3x-DIS data sheet."""

from __future__ import annotations

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patchbay.chips import sht3x as s
from patchbay.clock import FakeClock
from patchbay.i2c_bus import I2cBus, Segment
from patchbay.virtio_i2c import FLAGS_FAIL_NEXT, FLAGS_M_RD, MSG_OK, VirtioI2c
from tests.ringkit import Arena, DriverRing

FLAGS_NONE = 0


def hdr(flags: int) -> bytes:
    return struct.pack("<HHI", 0x44 << 1, 0, flags)


def setup() -> tuple[FakeClock, s.Sht3x, I2cBus]:
    clock = FakeClock(10.0)
    chip = s.Sht3x(clock)
    bus = I2cBus()
    bus.attach(chip)
    return clock, chip, bus


def cmd(bus: I2cBus, c: int) -> bool:
    return bus.transfer([Segment(0x44, False, bytes([c >> 8, c & 0xFF]))])[0].ok


def rd(bus: I2cBus, n: int = 6) -> bytes | None:
    r = bus.transfer([Segment(0x44, True, length=n)])[0]
    return r.data if r.ok else None


def test_crc8_matches_the_data_sheet_example() -> None:
    assert s.crc8(b"\xbe\xef") == 0x92


def test_crc8_of_a_word_with_its_crc_appended_verifies() -> None:
    w = s.word_with_crc(0x6666)
    assert s.crc8(w[:2]) == w[2]


@given(c=st.floats(min_value=-45.0, max_value=130.0))
def test_temperature_conversion_inverts_within_one_lsb(c: float) -> None:
    raw = s.raw_temperature(c)
    assert abs((-45.0 + 175.0 * raw / 65535.0) - c) <= 175.0 / 65535.0


@given(rh=st.floats(min_value=0.0, max_value=100.0))
def test_humidity_conversion_inverts_within_one_lsb(rh: float) -> None:
    raw = s.raw_humidity(rh)
    assert abs(100.0 * raw / 65535.0 - rh) <= 100.0 / 65535.0


def test_without_clock_stretching_a_read_before_the_measurement_is_done_is_not_acknowledged() -> None:
    clock, _, bus = setup()
    assert cmd(bus, 0x2400)
    clock.advance(0.010)
    assert rd(bus) is None
    clock.advance(0.003)
    data = rd(bus)
    assert data is not None and len(data) == 6
    assert s.crc8(data[0:2]) == data[2] and s.crc8(data[3:5]) == data[5]


def test_with_clock_stretching_an_early_read_succeeds_but_holds_the_bus_until_the_measurement_is_done() -> None:
    clock, chip, bus = setup()
    assert cmd(bus, 0x2C06)  # the command the QNX sample sends
    started = clock.now()
    clock.advance(0.001)
    res = bus.transfer([Segment(0x44, True, length=6)])[0]
    assert res.ok and len(res.data) == 6
    assert res.hold_until == pytest.approx(started + 0.0125)
    assert chip.measurements == 1


def test_a_read_after_the_measurement_time_does_not_hold_the_bus() -> None:
    clock, _, bus = setup()
    cmd(bus, 0x2C06)
    clock.advance(0.0125)
    assert bus.transfer([Segment(0x44, True, length=6)])[0].hold_until is None


def stretching_rig() -> tuple[FakeClock, VirtioI2c, Arena, DriverRing]:
    clock, _, bus = setup()
    a = Arena()
    return clock, VirtioI2c(bus, bus.faults, clock), a, DriverRing(a, size=8)


def test_a_stretching_read_is_incomplete_before_12_5_ms_and_complete_at_that_time() -> None:
    clock, dev, a, d = stretching_rig()
    ring = d.device()
    _, cmd_ins = d.add([hdr(FLAGS_NONE), bytes([0x2C, 0x06])], [1])
    assert len(dev.kick(ring)) == 1  # the command itself is acknowledged at once
    sent = clock.now()
    _, ins = d.add([hdr(FLAGS_M_RD)], [7])
    assert dev.kick(ring) == []
    assert dev.next_deadline() == pytest.approx(sent + 0.0125)
    clock.advance(0.0124)
    assert dev.poll() == []
    clock.advance(0.0001)
    done = dev.poll()
    assert len(done) == 1
    reply = a.read(ins[0], 7)
    assert reply[-1] == MSG_OK and s.crc8(reply[0:2]) == reply[2] and s.crc8(reply[3:5]) == reply[5]
    assert dev.next_deadline() is None
    assert a.read(cmd_ins[0], 1) == bytes([MSG_OK])


def test_a_command_and_a_stretching_read_in_one_group_complete_together_at_12_5_ms() -> None:
    clock, dev, a, d = stretching_rig()
    ring = d.device()
    sent = clock.now()
    d.add([hdr(FLAGS_FAIL_NEXT), bytes([0x2C, 0x06])], [1])
    _, ins = d.add([hdr(FLAGS_M_RD)], [7])
    assert dev.kick(ring) == []
    clock.advance(0.0124)
    assert dev.poll() == []
    clock.advance(0.0001)
    assert len(dev.poll()) == 2
    assert clock.now() >= sent + 0.0125 and a.read(ins[0], 7)[-1] == MSG_OK


def test_a_request_queued_behind_a_stretching_read_waits_for_it() -> None:
    clock, dev, a, d = stretching_rig()
    ring = d.device()
    d.add([hdr(FLAGS_NONE), bytes([0x2C, 0x06])], [1])
    dev.kick(ring)
    d.add([hdr(FLAGS_M_RD)], [7])
    d.add([hdr(FLAGS_NONE)], [1])  # an address probe, queued behind the read
    assert dev.kick(ring) == []
    clock.advance(0.0125)
    assert len(dev.poll()) == 2


def test_data_is_read_once() -> None:
    clock, _, bus = setup()
    cmd(bus, 0x2400)
    clock.advance(0.02)
    assert rd(bus) is not None
    assert rd(bus) is None


def test_the_reading_is_the_modelled_room() -> None:
    clock, chip, bus = setup()
    cmd(bus, 0x2400)
    clock.advance(0.02)
    data = rd(bus)
    assert data is not None
    t = -45.0 + 175.0 * int.from_bytes(data[0:2], "big") / 65535.0
    rh = 100.0 * int.from_bytes(data[3:5], "big") / 65535.0
    want_t, want_rh = chip.climate(clock.now() - chip.t0)
    assert t == pytest.approx(want_t, abs=0.01) and rh == pytest.approx(want_rh, abs=0.01)


def test_soft_reset_makes_the_sensor_deaf_briefly_and_sets_the_reset_status_bit() -> None:
    clock, _, bus = setup()
    cmd(bus, s.CLEAR_STATUS)
    assert cmd(bus, s.SOFT_RESET)
    assert not cmd(bus, 0x2400)
    clock.advance(s.RESET_TIME_S)
    assert cmd(bus, s.READ_STATUS)
    status = rd(bus, 3)
    assert status is not None and int.from_bytes(status[:2], "big") & s.STATUS_RESET


def test_an_unknown_command_is_not_acknowledged_and_flags_the_status() -> None:
    _, chip, bus = setup()
    assert not cmd(bus, 0x1234)
    assert chip.status & s.STATUS_CMD_FAILED


def test_a_short_read_returns_the_first_bytes() -> None:
    clock, _, bus = setup()
    cmd(bus, 0x2400)
    clock.advance(0.02)
    data = rd(bus, 3)
    assert data is not None and len(data) == 3 and s.crc8(data[:2]) == data[2]
