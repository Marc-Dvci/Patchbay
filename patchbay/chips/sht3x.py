"""Sensirion SHT3x humidity and temperature sensor, I2C interface.

Source: Sensirion, "Datasheet SHT3x-DIS". What follows from it:
  * 16-bit commands sent MSB first (section 4.3); single-shot measurements with clock stretching enabled
    (0x2C06, 0x2C0D, 0x2C10) or disabled (0x2400, 0x240B, 0x2416), soft reset 0x30A2, break 0x3093, read status
    0xF32D, clear status 0x3041 (tables 9 to 15);
  * typical measurement durations of 12.5, 4.5 and 2.5 ms for high, medium and low repeatability (table 4);
  * with clock stretching disabled, a read before the measurement is done is not acknowledged (section 4.4). With it
    enabled the sensor holds SCL until the data is ready, so the read succeeds later rather than failing;
  * each 16-bit word is followed by a CRC-8: polynomial 0x31, initial value 0xFF, so CRC(0xBEEF) = 0x92 (section 4.12);
  * T = -45 + 175 * S_T / 65535 degrees Celsius and RH = 100 * S_RH / 65535 % (section 4.13);
  * after a soft reset the sensor answers again within 1.5 ms (table 5), and status bit 4 reports the reset.
Modelling choices: the room sits at 22.5 degrees Celsius and 45 % RH with a slow drift; the typical soft-reset time,
0.5 ms, gates the answer; heater and alert features are storage only.
"""

from __future__ import annotations

import math

from ..clock import Clock
from ..i2c_bus import I2cChip

SINGLE_SHOT = {
    0x2C06: (True, 0.0125),
    0x2C0D: (True, 0.0045),
    0x2C10: (True, 0.0025),
    0x2400: (False, 0.0125),
    0x240B: (False, 0.0045),
    0x2416: (False, 0.0025),
}
SOFT_RESET = 0x30A2
BREAK = 0x3093
READ_STATUS = 0xF32D
CLEAR_STATUS = 0x3041
HEATER_ON = 0x306D
HEATER_OFF = 0x3066
RESET_TIME_S = 0.0005

STATUS_ALERT = 1 << 15
STATUS_HEATER = 1 << 13
STATUS_RESET = 1 << 4
STATUS_CMD_FAILED = 1 << 1


def crc8(data: bytes) -> int:
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def word_with_crc(value: int) -> bytes:
    w = bytes([(value >> 8) & 0xFF, value & 0xFF])
    return w + bytes([crc8(w)])


def raw_temperature(celsius: float) -> int:
    return max(0, min(0xFFFF, round((celsius + 45.0) * 65535.0 / 175.0)))


def raw_humidity(percent: float) -> int:
    return max(0, min(0xFFFF, round(percent * 65535.0 / 100.0)))


class Sht3x(I2cChip):
    name = "SHT3x"

    def __init__(self, clock: Clock, address: int = 0x44) -> None:
        if address not in (0x44, 0x45):
            raise ValueError("the SHT3x answers at 0x44 or 0x45 (ADDR pin)")
        self.address = address
        self.clock = clock
        self.t0 = clock.now()
        self.status = STATUS_ALERT | STATUS_RESET
        self.ready_at = clock.now()
        self.pending: tuple[float, bool] | None = None  # (done at, clock stretching)
        self.out: bytes = b""
        self.measurements = 0

    def climate(self, t: float) -> tuple[float, float]:
        return (22.5 + 0.3 * math.sin(t / 60.0), 45.0 + 2.0 * math.sin(t / 90.0))

    def acks_address(self, read: bool) -> bool:
        now = self.clock.now()
        if now < self.ready_at:
            return False
        if not read:
            return True
        if self.out:
            return True
        if self.pending is not None:
            done_at, stretching = self.pending
            if now >= done_at or stretching:
                self._finish_measurement(max(now, done_at))
                return True
            return False
        return False  # no data to read

    def _finish_measurement(self, at: float) -> None:
        t, rh = self.climate(at - self.t0)
        self.out = word_with_crc(raw_temperature(t)) + word_with_crc(raw_humidity(rh))
        self.pending = None
        self.measurements += 1

    def write(self, data: bytes) -> bool:
        if len(data) == 0:
            return True
        if len(data) != 2:
            self.status |= STATUS_CMD_FAILED
            return len(data) == 1  # the second command byte never came; a longer write is not a command we model
        cmd = data[0] << 8 | data[1]
        now = self.clock.now()
        self.status &= ~STATUS_CMD_FAILED
        if cmd in SINGLE_SHOT:
            stretching, duration = SINGLE_SHOT[cmd]
            self.out = b""
            self.pending = (now + duration, stretching)
        elif cmd == SOFT_RESET:
            self.pending = None
            self.out = b""
            self.status = STATUS_ALERT | STATUS_RESET
            self.ready_at = now + RESET_TIME_S
        elif cmd == BREAK:
            self.pending = None
        elif cmd == READ_STATUS:
            self.out = word_with_crc(self.status)
        elif cmd == CLEAR_STATUS:
            self.status &= ~(STATUS_ALERT | STATUS_RESET)
        elif cmd == HEATER_ON:
            self.status |= STATUS_HEATER
        elif cmd == HEATER_OFF:
            self.status &= ~STATUS_HEATER
        else:
            self.status |= STATUS_CMD_FAILED
            return False
        return True

    def read(self, n: int) -> bytes:
        data = self.out[:n] + bytes(max(0, n - len(self.out)))
        self.out = b""
        return data
