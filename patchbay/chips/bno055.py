"""Bosch BNO055 9-axis absolute orientation sensor, I2C interface.

Source: Bosch Sensortec, "BNO055 Intelligent 9-axis absolute orientation sensor", data sheet BST-BNO055-DS000.
What follows from it:
  * register map page 0 (section 4.3) with the identification values CHIP_ID 0xA0, ACC_ID 0xFB, MAG_ID 0x32,
    GYR_ID 0x0F, and PAGE_ID at 0x07 selecting page 0 or 1;
  * the register address auto-increments on burst reads and writes (section 4.6);
  * CONFIGMODE is the operating mode after power-on and reset (section 3.3). Outside CONFIGMODE only OPR_MODE (and the
    interrupt registers) can be written; other writes are ignored (section 3.3);
  * the operating modes and the sensors each one powers (section 3.3, table 3-5): fusion outputs (Euler angles,
    quaternion, linear acceleration, gravity) exist only in the fusion modes IMU, COMPASS, M4G, NDOF_FMC_OFF and NDOF;
  * SYS_TRIGGER bit 5 (RST_SYS) resets the chip; it answers again after the 650 ms start-up time (section 1.2), and
    does not acknowledge its address meanwhile;
  * output scaling with UNIT_SEL at its reset value: 1 m/s^2 = 100 LSB, 1 uT = 16 LSB, 1 dps = 16 LSB, 1 degree =
    16 LSB, quaternion 1 = 2^14 LSB (section 3.6);
  * CALIB_STAT packs SYS, GYR, ACC and MAG levels 0 to 3 in bits 7:6, 5:4, 3:2, 1:0 (section 4.3.54).
Modelling choices, not from the data sheet:
  * motion comes from a scripted turntable: heading turns at 10 degrees per second, roll and pitch rock gently;
  * calibration rises one level per sensor every `calib_step_s` seconds in a fusion mode, so CALIB_STAT reaches 0xFF;
  * gyroscope output is the rate of the Euler angles, which equals the body rate only for small roll and pitch;
  * page 1 is plain storage; mode-switch times (7 ms and 19 ms) and clock stretching are not modelled.
"""

from __future__ import annotations

import math
import struct

from ..clock import Clock
from ..i2c_bus import I2cChip

CHIP_ID = 0x00
PAGE_ID = 0x07
ACC_DATA = 0x08
MAG_DATA = 0x0E
GYR_DATA = 0x14
EUL_DATA = 0x1A
QUA_DATA = 0x20
LIA_DATA = 0x28
GRV_DATA = 0x2E
TEMP = 0x34
CALIB_STAT = 0x35
ST_RESULT = 0x36
SYS_STATUS = 0x39
UNIT_SEL = 0x3B
OPR_MODE = 0x3D
PWR_MODE = 0x3E
SYS_TRIGGER = 0x3F
AXIS_MAP_CONFIG = 0x41
AXIS_MAP_SIGN = 0x42
OFFSETS = 0x55  # 22 bytes, to 0x6A
OFFSETS_LEN = 22
INT_MSK = 0x0F  # page 1
INT_EN = 0x10  # page 1

MODE_CONFIG = 0x00
FUSION_MODES = frozenset({0x08, 0x09, 0x0A, 0x0B, 0x0C})
ACC_MODES = frozenset({0x01, 0x04, 0x05, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C})
MAG_MODES = frozenset({0x02, 0x04, 0x06, 0x07, 0x09, 0x0A, 0x0B, 0x0C})
GYR_MODES = frozenset({0x03, 0x05, 0x06, 0x07, 0x08, 0x0B, 0x0C})

STARTUP_S = 0.650
GRAVITY = 9.80665
EARTH_FIELD_UT = (22.0, 0.0, -42.0)  # north, east, down-ish: a mid-latitude field, a modelling choice

RESET_PAGE0 = {
    CHIP_ID: 0xA0,
    0x01: 0xFB,
    0x02: 0x32,
    0x03: 0x0F,
    0x04: 0x11,
    0x05: 0x03,
    0x06: 0x15,
    ST_RESULT: 0x0F,
    UNIT_SEL: 0x80,
    AXIS_MAP_CONFIG: 0x24,
    AXIS_MAP_SIGN: 0x00,
}
READ_ONLY = frozenset(range(0x00, 0x07)) | frozenset(range(0x08, 0x3B))


def euler_to_quaternion(heading: float, roll: float, pitch: float) -> tuple[float, float, float, float]:
    """Unit quaternion (w, x, y, z) for yaw = heading, then pitch, then roll, in degrees."""
    y, p, r = (math.radians(a) / 2 for a in (heading, pitch, roll))
    cy, sy, cp, sp, cr, sr = math.cos(y), math.sin(y), math.cos(p), math.sin(p), math.cos(r), math.sin(r)
    return (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )


def rotate_into_body(q: tuple[float, float, float, float], v: tuple[float, float, float]) -> tuple[float, float, float]:
    """World vector v seen in the sensor frame: conj(q) * v * q."""
    w, x, y, z = q
    # rotation matrix of q, transposed
    m = (
        (1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)),
        (2 * (x * y - w * z), 1 - 2 * (x * x + z * z), 2 * (y * z + w * x)),
        (2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)),
    )
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))  # type: ignore[return-value]


def _s16(v: float) -> int:
    return max(-32768, min(32767, int(round(v))))


class Bno055(I2cChip):
    name = "BNO055"

    def __init__(self, clock: Clock, address: int = 0x28, calib_step_s: float = 0.5) -> None:
        if address not in (0x28, 0x29):
            raise ValueError("the BNO055 answers at 0x28 or 0x29 (COM3 pin)")
        self.address = address
        self.clock = clock
        self.calib_step_s = calib_step_s
        self.t0 = clock.now()
        self._reset(ready_at=clock.now())

    # --- state -----------------------------------------------------------------------------------------------------
    def _reset(self, ready_at: float) -> None:
        self.page0 = bytearray(0x80)
        self.page1 = bytearray(0x80)
        for reg, val in RESET_PAGE0.items():
            self.page0[reg] = val
        self.page1[PAGE_ID] = 1
        self.pointer = 0
        self.ready_at = ready_at
        self.fusion_since: float | None = None
        self.addressed_write = False

    @property
    def mode(self) -> int:
        return self.page0[OPR_MODE] & 0x0F

    @property
    def page(self) -> int:
        return self.page0[PAGE_ID] & 1

    def regs(self) -> bytearray:
        return self.page1 if self.page else self.page0

    # --- motion ----------------------------------------------------------------------------------------------------
    def orientation(self, t: float) -> tuple[float, float, float]:
        """(heading, roll, pitch) in degrees at t seconds after power-on."""
        return ((10.0 * t) % 360.0, 8.0 * math.sin(0.7 * t), 5.0 * math.sin(0.45 * t))

    def calib_levels(self, now: float) -> int:
        if self.fusion_since is None:
            return 0
        level = min(3, int((now - self.fusion_since) / self.calib_step_s))
        return level << 6 | level << 4 | level << 2 | level

    def _refresh(self, now: float) -> None:
        mode = self.mode
        r = self.page0
        if mode == MODE_CONFIG:
            r[SYS_STATUS] = 0
            return
        t = now - self.t0
        h, roll, pitch = self.orientation(t)
        q = euler_to_quaternion(h, roll, pitch)
        grv = rotate_into_body(q, (0.0, 0.0, GRAVITY))
        lia = (0.2 * math.sin(1.3 * t), 0.1 * math.cos(1.1 * t), 0.0)
        acc = tuple(g + a for g, a in zip(grv, lia, strict=True))
        mag = rotate_into_body(q, EARTH_FIELD_UT)
        rates = (10.0, 8.0 * 0.7 * math.cos(0.7 * t), 5.0 * 0.45 * math.cos(0.45 * t))  # heading, roll, pitch
        gyr = (rates[1], rates[2], rates[0])
        if mode in ACC_MODES:
            struct.pack_into("<3h", r, ACC_DATA, *(_s16(v * 100) for v in acc))
        if mode in MAG_MODES:
            struct.pack_into("<3h", r, MAG_DATA, *(_s16(v * 16) for v in mag))
        if mode in GYR_MODES:
            struct.pack_into("<3h", r, GYR_DATA, *(_s16(v * 16) for v in gyr))
        if mode in FUSION_MODES:
            struct.pack_into("<3h", r, EUL_DATA, _s16(h * 16), _s16(roll * 16), _s16(pitch * 16))
            struct.pack_into("<4h", r, QUA_DATA, *(_s16(c * (1 << 14)) for c in q))
            struct.pack_into("<3h", r, LIA_DATA, *(_s16(v * 100) for v in lia))
            struct.pack_into("<3h", r, GRV_DATA, *(_s16(v * 100) for v in grv))
            r[CALIB_STAT] = self.calib_levels(now)
            r[SYS_STATUS] = 5
        else:
            r[SYS_STATUS] = 6
        r[TEMP] = 23

    # --- bus -------------------------------------------------------------------------------------------------------
    def acks_address(self, read: bool) -> bool:
        now = self.clock.now()
        if now < self.ready_at:
            return False
        if read:
            self._refresh(now)
        self.addressed_write = not read
        return True

    def write(self, data: bytes) -> bool:
        if not data:
            return True
        self.pointer = data[0] & 0x7F
        for b in data[1:]:
            self._write_reg(self.pointer, b)
            if self.ready_at > self.clock.now():  # a reset in the middle of a burst ends it
                return True
            self.pointer = (self.pointer + 1) & 0x7F
        return True

    def read(self, n: int) -> bytes:
        regs = self.regs()
        out = bytearray()
        for _ in range(n):
            out.append(regs[self.pointer])
            self.pointer = (self.pointer + 1) & 0x7F
        return bytes(out)

    def _write_reg(self, reg: int, val: int) -> None:
        now = self.clock.now()
        if self.page == 0 and reg == OPR_MODE:
            old = self.mode
            self.page0[OPR_MODE] = val & 0x0F
            new = val & 0x0F
            if new in FUSION_MODES and old not in FUSION_MODES:
                self.fusion_since = now
            elif new not in FUSION_MODES:
                self.fusion_since = None
            return
        if reg == PAGE_ID:
            self.page0[PAGE_ID] = val & 1
            self.page1[PAGE_ID] = 1
            return
        if self.page == 1 and reg in (INT_MSK, INT_EN):
            self.page1[reg] = val
            return
        if self.mode != MODE_CONFIG:
            return  # ignored outside CONFIGMODE
        if self.page == 1:
            self.page1[reg] = val
            return
        if reg in READ_ONLY:
            return
        if reg == SYS_TRIGGER:
            if val & 0x20:
                self._reset(ready_at=now + STARTUP_S)
                return
            self.page0[SYS_TRIGGER] = val & 0xC1
            return
        self.page0[reg] = val
