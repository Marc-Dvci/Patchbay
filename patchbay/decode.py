"""What a bus transaction means to the chip at the other end, in one line, from the same register maps the chip
models use. The panel shows these lines next to the raw bytes.

  describe_i2c([(addr, read, data, ok), ...])   one combined transaction (a FAIL_NEXT group)
  describe_spi(cs, tx, rx, ok)                  one chip-select assertion
"""

from __future__ import annotations

import struct

from .chips import bno055 as b
from .chips import sht3x as s

I2C_NAMES = {0x28: "BNO055", 0x29: "BNO055", 0x44: "SHT3x", 0x45: "SHT3x"}
SPI_NAMES = {0: "MCP3008"}

BNO_REGS = {
    b.CHIP_ID: "CHIP_ID",
    b.PAGE_ID: "PAGE_ID",
    b.ACC_DATA: "ACC_DATA",
    b.MAG_DATA: "MAG_DATA",
    b.GYR_DATA: "GYR_DATA",
    b.EUL_DATA: "EUL_DATA",
    b.QUA_DATA: "QUA_DATA",
    b.LIA_DATA: "LIA_DATA",
    b.GRV_DATA: "GRV_DATA",
    b.TEMP: "TEMP",
    b.CALIB_STAT: "CALIB_STAT",
    b.SYS_STATUS: "SYS_STATUS",
    b.UNIT_SEL: "UNIT_SEL",
    b.OPR_MODE: "OPR_MODE",
    b.PWR_MODE: "PWR_MODE",
    b.SYS_TRIGGER: "SYS_TRIGGER",
    b.OFFSETS: "ACC_OFFSET_X_LSB (calibration block)",
}
BNO_MODES = {
    0x00: "CONFIGMODE",
    0x01: "ACCONLY",
    0x02: "MAGONLY",
    0x03: "GYROONLY",
    0x04: "ACCMAG",
    0x05: "ACCGYRO",
    0x06: "MAGGYRO",
    0x07: "AMG",
    0x08: "IMU",
    0x09: "COMPASS",
    0x0A: "M4G",
    0x0B: "NDOF_FMC_OFF",
    0x0C: "NDOF",
}
VECTORS = {
    b.ACC_DATA: ("accel", 100.0, "m/s²"),
    b.MAG_DATA: ("mag", 16.0, "µT"),
    b.GYR_DATA: ("gyro", 16.0, "dps"),
    b.LIA_DATA: ("linear accel", 100.0, "m/s²"),
    b.GRV_DATA: ("gravity", 100.0, "m/s²"),
}
SHT_CMDS = {
    0x2C06: "measure, high repeatability, clock stretching",
    0x2C0D: "measure, medium repeatability, clock stretching",
    0x2C10: "measure, low repeatability, clock stretching",
    0x2400: "measure, high repeatability",
    0x240B: "measure, medium repeatability",
    0x2416: "measure, low repeatability",
    s.SOFT_RESET: "soft reset",
    s.BREAK: "break",
    s.READ_STATUS: "read status",
    s.CLEAR_STATUS: "clear status",
    s.HEATER_ON: "heater on",
    s.HEATER_OFF: "heater off",
}


def _bno_read(reg: int, data: bytes) -> str:
    name = BNO_REGS.get(reg, f"reg 0x{reg:02X}")
    if reg == b.CHIP_ID and data:
        rest = f", ACC_ID 0x{data[1]:02X}" if len(data) > 1 else ""
        return f"CHIP_ID = 0x{data[0]:02X}{rest}"
    if reg == b.CALIB_STAT and data:
        c = data[0]
        return f"CALIB_STAT sys {c >> 6 & 3}, gyr {c >> 4 & 3}, acc {c >> 2 & 3}, mag {c & 3}"
    if reg == b.EUL_DATA and len(data) >= 6:
        h, r, p = struct.unpack_from("<3h", data)
        return f"EUL_DATA heading {h / 16:.2f}°, roll {r / 16:.2f}°, pitch {p / 16:.2f}°"
    if reg == b.QUA_DATA and len(data) >= 8:
        q = struct.unpack_from("<4h", data)
        return "QUA_DATA " + ", ".join(f"{n} {v / 16384:.3f}" for n, v in zip("wxyz", q, strict=True))
    if reg in VECTORS and len(data) >= 6:
        label, div, unit = VECTORS[reg]
        x, y, z = struct.unpack_from("<3h", data)
        return f"{name} {label} {x / div:.2f}, {y / div:.2f}, {z / div:.2f} {unit}"
    if reg == b.OPR_MODE and data:
        return f"OPR_MODE = {BNO_MODES.get(data[0] & 0x0F, hex(data[0]))}"
    return f"{name}: {len(data)} byte(s)"


def _bno_write(data: bytes) -> str:
    reg = data[0]
    name = BNO_REGS.get(reg, f"reg 0x{reg:02X}")
    vals = data[1:]
    if not vals:
        return f"select {name}"
    if reg == b.OPR_MODE:
        return f"OPR_MODE ← {BNO_MODES.get(vals[0] & 0x0F, hex(vals[0]))}"
    if reg == b.SYS_TRIGGER and vals[0] & 0x20:
        return "SYS_TRIGGER ← reset (650 ms start-up)"
    if reg == b.OFFSETS:
        return f"write calibration block, {len(vals)} bytes"
    return f"{name} ← " + " ".join(f"0x{v:02X}" for v in vals)


def _sht_read(data: bytes) -> str:
    if len(data) >= 6:
        t_ok = s.crc8(data[0:2]) == data[2]
        h_ok = s.crc8(data[3:5]) == data[5]
        t = -45.0 + 175.0 * int.from_bytes(data[0:2], "big") / 65535.0
        rh = 100.0 * int.from_bytes(data[3:5], "big") / 65535.0
        crc = "CRC ok" if t_ok and h_ok else "CRC MISMATCH: corrupted on the bus"
        return f"T {t:.2f} °C, RH {rh:.2f} %, {crc}"
    if len(data) >= 3:
        ok = "CRC ok" if s.crc8(data[0:2]) == data[2] else "CRC MISMATCH"
        return f"status 0x{int.from_bytes(data[0:2], 'big'):04X}, {ok}"
    return f"{len(data)} byte(s)"


def describe_i2c(segs: list[tuple[int, bool, bytes, bool]]) -> str:
    """segs: (7-bit address, read, bytes written or read, acknowledged)."""
    if not segs:
        return ""
    addr = segs[0][0]
    chip = I2C_NAMES.get(addr)
    who = f"{chip} @0x{addr:02X}" if chip else f"0x{addr:02X} (nothing modelled)"
    if not all(ok for *_, ok in segs):
        return f"{who}: no acknowledge"
    if chip == "BNO055":
        if len(segs) == 2 and not segs[0][1] and segs[1][1] and len(segs[0][2]) == 1:
            return f"{who} {_bno_read(segs[0][2][0], segs[1][2])}"
        if len(segs) == 1 and not segs[0][1] and segs[0][2]:
            return f"{who} {_bno_write(segs[0][2])}"
    if chip == "SHT3x":
        if len(segs) == 1 and not segs[0][1] and len(segs[0][2]) == 2:
            cmd = int.from_bytes(segs[0][2], "big")
            return f"{who} {SHT_CMDS.get(cmd, f'command 0x{cmd:04X}')}"
        if len(segs) == 1 and segs[0][1]:
            return f"{who} {_sht_read(segs[0][2])}"
    return who


def describe_spi(cs: int, tx: bytes, rx: bytes, ok: bool) -> str:
    chip = SPI_NAMES.get(cs)
    who = f"{chip} cs{cs}" if chip else f"cs{cs} (nothing modelled)"
    if not ok:
        return f"{who}: transfer refused"
    if chip == "MCP3008" and len(tx) == 3 and tx[0] & 1 and len(rx) == 3:
        single = bool(tx[1] & 0x80)
        ch = (tx[1] >> 4) & 7
        code = (rx[1] & 3) << 8 | rx[2]
        mode = "single-ended" if single else "differential"
        return f"{who} CH{ch} {mode} → {code} ({code * 3.3 / 1024:.2f} V)"
    return who
