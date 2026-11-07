"""The BNO055 model against the behaviour its data sheet (BST-BNO055-DS000) describes."""

from __future__ import annotations

import math
import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patchbay.chips import bno055 as b
from patchbay.clock import FakeClock
from patchbay.i2c_bus import I2cBus, Segment


def read(bus: I2cBus, reg: int, n: int, addr: int = 0x28) -> bytes | None:
    r = bus.transfer([Segment(addr, False, bytes([reg])), Segment(addr, True, length=n)])
    return r[1].data if all(x.ok for x in r) else None


def write(bus: I2cBus, reg: int, *vals: int, addr: int = 0x28) -> bool:
    return bus.transfer([Segment(addr, False, bytes([reg, *vals]))])[0].ok


def setup(**kw: float) -> tuple[FakeClock, b.Bno055, I2cBus]:
    clock = FakeClock(100.0)
    chip = b.Bno055(clock, **kw)
    bus = I2cBus()
    bus.attach(chip)
    return clock, chip, bus


def test_identification_registers_hold_the_data_sheet_values() -> None:
    _, _, bus = setup()
    assert read(bus, b.CHIP_ID, 4) == bytes([0xA0, 0xFB, 0x32, 0x0F])


def test_power_on_mode_is_configmode_and_power_on_self_test_passed() -> None:
    _, chip, bus = setup()
    assert chip.mode == b.MODE_CONFIG
    assert read(bus, b.ST_RESULT, 1) == b"\x0f"
    assert read(bus, b.UNIT_SEL, 1) == b"\x80"


def test_burst_reads_auto_increment_and_wrap_at_the_end_of_the_page() -> None:
    _, chip, bus = setup()
    assert read(bus, 0x7F, 2) == bytes([chip.page0[0x7F], 0xA0])


def test_only_opr_mode_is_writable_outside_configmode() -> None:
    _, chip, bus = setup()
    assert write(bus, b.OPR_MODE, 0x0C)
    assert write(bus, b.UNIT_SEL, 0x01)
    assert chip.page0[b.UNIT_SEL] == 0x80
    assert write(bus, b.OPR_MODE, b.MODE_CONFIG)
    assert write(bus, b.UNIT_SEL, 0x01)
    assert chip.page0[b.UNIT_SEL] == 0x01


def test_identification_registers_ignore_writes() -> None:
    _, _, bus = setup()
    write(bus, b.CHIP_ID, 0x00)
    assert read(bus, b.CHIP_ID, 1) == b"\xa0"


def test_reset_nacks_for_650_ms_then_answers_with_defaults() -> None:
    clock, chip, bus = setup()
    write(bus, b.OFFSETS, 0x12)
    assert chip.page0[b.OFFSETS] == 0x12
    assert write(bus, b.SYS_TRIGGER, 0x20)
    assert read(bus, b.CHIP_ID, 1) is None
    clock.advance(0.649)
    assert read(bus, b.CHIP_ID, 1) is None
    clock.advance(0.002)
    assert read(bus, b.CHIP_ID, 1) == b"\xa0"
    assert chip.page0[b.OFFSETS] == 0


def test_sys_trigger_reset_is_ignored_outside_configmode() -> None:
    _, _, bus = setup()
    write(bus, b.OPR_MODE, 0x0C)
    write(bus, b.SYS_TRIGGER, 0x20)
    assert read(bus, b.CHIP_ID, 1) == b"\xa0"


def test_calibration_rises_to_0xff_in_a_fusion_mode_and_drops_to_zero_outside() -> None:
    clock, _, bus = setup(calib_step_s=0.5)
    write(bus, b.OPR_MODE, 0x0C)
    assert read(bus, b.CALIB_STAT, 1) == b"\x00"
    clock.advance(0.5)
    assert read(bus, b.CALIB_STAT, 1) == bytes([0b01010101])
    clock.advance(1.0)
    assert read(bus, b.CALIB_STAT, 1) == b"\xff"
    write(bus, b.OPR_MODE, 0x01)  # ACCONLY: no fusion
    write(bus, b.OPR_MODE, 0x0C)
    assert read(bus, b.CALIB_STAT, 1) == b"\x00"


def test_fusion_registers_stay_zero_in_a_non_fusion_mode() -> None:
    clock, _, bus = setup()
    write(bus, b.OPR_MODE, 0x07)  # AMG: all sensors, no fusion
    clock.advance(1.0)
    assert read(bus, b.EUL_DATA, 6) == bytes(6)
    assert read(bus, b.ACC_DATA, 6) != bytes(6)


def test_page_1_is_selected_by_page_id() -> None:
    _, _, bus = setup()
    write(bus, b.PAGE_ID, 1)
    assert read(bus, b.PAGE_ID, 1) == b"\x01"
    assert read(bus, b.CHIP_ID, 1) == b"\x00"
    write(bus, b.PAGE_ID, 0)
    assert read(bus, b.CHIP_ID, 1) == b"\xa0"


def test_the_address_follows_the_com3_pin() -> None:
    clock = FakeClock()
    assert b.Bno055(clock, address=0x29).address == 0x29
    with pytest.raises(ValueError):
        b.Bno055(clock, address=0x30)


@given(t=st.floats(min_value=0.0, max_value=3600.0, allow_nan=False))
def test_ndof_outputs_are_physically_consistent_at_any_time(t: float) -> None:
    clock, _, bus = setup()
    write(bus, b.OPR_MODE, 0x0C)
    clock.advance(t)
    raw = read(bus, b.ACC_DATA, 0x34 - b.ACC_DATA)
    assert raw is not None
    acc = struct.unpack_from("<3h", raw, 0)
    quat = struct.unpack_from("<4h", raw, b.QUA_DATA - b.ACC_DATA)
    lia = struct.unpack_from("<3h", raw, b.LIA_DATA - b.ACC_DATA)
    grv = struct.unpack_from("<3h", raw, b.GRV_DATA - b.ACC_DATA)
    norm_q = math.sqrt(sum((c / 16384.0) ** 2 for c in quat))
    assert abs(norm_q - 1.0) < 4 / 16384.0  # rounding of four components
    norm_g = math.sqrt(sum((c / 100.0) ** 2 for c in grv))
    assert abs(norm_g - b.GRAVITY) < 0.02
    for a_, g_, l_ in zip(acc, grv, lia, strict=True):
        assert abs(a_ - (g_ + l_)) <= 2  # each was rounded to 1 LSB on its own
    heading = struct.unpack_from("<h", raw, b.EUL_DATA - b.ACC_DATA)[0] / 16.0
    assert 0.0 <= heading < 360.0 + 1 / 16


def test_euler_to_quaternion_matches_known_rotations() -> None:
    assert b.euler_to_quaternion(0, 0, 0) == pytest.approx((1, 0, 0, 0))
    w, x, y, z = b.euler_to_quaternion(90, 0, 0)
    assert (w, x, y, z) == pytest.approx((math.sqrt(0.5), 0, 0, math.sqrt(0.5)))
    gx, gy, gz = b.rotate_into_body(b.euler_to_quaternion(0, 90, 0), (0.0, 0.0, 1.0))
    assert (gx, gy, gz) == pytest.approx((0.0, 1.0, 0.0), abs=1e-9)
