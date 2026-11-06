"""Microchip MCP3008 8-channel 10-bit ADC, SPI interface.

Source: Microchip, "MCP3004/3008 2.7V 4-Channel/8-Channel 10-Bit A/D Converters with SPI Serial Interface",
DS21295. What follows from it, bit by bit on the wire with chip select held low for one exchange (section 5, figure 6-1):
  * the first 1 on DIN is the start bit; the next four bits are SGL/DIFF, D2, D1, D0 (table 5-2);
  * one more clock samples the input; on the next one DOUT drives a null bit (0);
  * DOUT then gives B9..B0, most significant bit first, then B1..B9 again least significant first, then zeros;
  * before the null bit DOUT is high impedance. The model drives 0 there, which is what the QNX sample masks off;
  * code = 1024 * VIN / VREF, clipped to 0..1023 (section 4.1); differential pairs per table 5-2, negative reads 0.
Modelling choices: VREF = 3.3 V; channels 0 and 1 carry a joystick moving slowly in a circle, the others 0 V; clock
polarity and phase are accepted as configured (the part supports modes 0,0 and 1,1).
"""

from __future__ import annotations

import math
from collections.abc import Callable

from ..clock import Clock

DIFF_PAIRS = {0: (0, 1), 1: (1, 0), 2: (2, 3), 3: (3, 2), 4: (4, 5), 5: (5, 4), 6: (6, 7), 7: (7, 6)}


def joystick(t: float) -> list[float]:
    return [1.65 + 1.3 * math.cos(0.8 * t), 1.65 + 1.3 * math.sin(0.8 * t)] + [0.0] * 6


def code_for(volts: float, vref: float) -> int:
    return max(0, min(1023, int(1024.0 * volts / vref)))


class Mcp3008:
    name = "MCP3008"

    def __init__(self, clock: Clock, vref: float = 3.3, inputs: Callable[[float], list[float]] = joystick) -> None:
        self.clock = clock
        self.vref = vref
        self.inputs = inputs
        self.t0 = clock.now()
        self.conversions = 0

    def sample(self, single: bool, channel: int) -> int:
        v = self.inputs(self.clock.now() - self.t0)
        if single:
            vin = v[channel]
        else:
            plus, minus = DIFF_PAIRS[channel]
            vin = v[plus] - v[minus]
        return code_for(vin, self.vref)

    def exchange(self, tx: bytes) -> bytes:
        """One chip-select assertion: the bytes on DOUT for the bytes on DIN, MSB first."""
        out_bits: list[int] = []
        state = "wait"
        cfg: list[int] = []
        code = 0
        stream: list[int] = []
        for byte in tx:
            for k in range(7, -1, -1):
                din = (byte >> k) & 1
                if state == "wait":
                    out_bits.append(0)
                    if din:
                        state = "config"
                elif state == "config":
                    out_bits.append(0)
                    cfg.append(din)
                    if len(cfg) == 4:
                        state = "sample"
                elif state == "sample":
                    out_bits.append(0)
                    single = cfg[0] == 1
                    channel = cfg[1] << 2 | cfg[2] << 1 | cfg[3]
                    code = self.sample(single, channel)
                    self.conversions += 1
                    msb = [(code >> i) & 1 for i in range(9, -1, -1)]
                    lsb = [(code >> i) & 1 for i in range(1, 10)]
                    stream = [0] + msb + lsb  # null bit, B9..B0, B1..B9
                    state = "data"
                else:
                    out_bits.append(stream.pop(0) if stream else 0)
        out = bytearray()
        for i in range(0, len(out_bits), 8):
            b = 0
            for bit in out_bits[i : i + 8]:
                b = b << 1 | bit
            out.append(b)
        return bytes(out)
