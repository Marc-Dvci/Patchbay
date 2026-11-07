"""The MCP3008 model against the Microchip DS21295 bit sequence."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from patchbay.chips.mcp3008 import Mcp3008, code_for
from patchbay.clock import FakeClock


def chip_with(levels: list[float]) -> Mcp3008:
    return Mcp3008(FakeClock(), vref=3.3, inputs=lambda t: levels)


def sample_frame(channel: int, single: bool = True) -> bytes:
    """The 3 bytes QNX's mcp-3008-joystick sample sends: start bit, then SGL/DIFF and the channel in bits 7-4."""
    return bytes([0x01, ((0x08 if single else 0x00) | channel) << 4, 0x00])


def decode(rx: bytes) -> int:
    return (rx[1] & 0x03) << 8 | rx[2]


@given(channel=st.integers(0, 7), volts=st.floats(min_value=0.0, max_value=3.3))
def test_the_samples_frame_returns_the_channels_code(channel: int, volts: float) -> None:
    levels = [0.0] * 8
    levels[channel] = volts
    rx = chip_with(levels).exchange(sample_frame(channel))
    assert decode(rx) == code_for(volts, 3.3)
    assert rx[0] == 0 and rx[1] & 0x04 == 0  # high impedance modelled as 0, then the null bit


def test_codes_follow_1024_vin_over_vref_and_clip() -> None:
    assert code_for(0.0, 3.3) == 0
    assert code_for(1.65, 3.3) == 512
    assert code_for(3.3, 3.3) == 1023
    assert code_for(5.0, 3.3) == 1023
    assert code_for(-1.0, 3.3) == 0


def test_differential_mode_reads_the_pair_and_clips_negative_to_zero() -> None:
    c = chip_with([2.0, 1.0, 0, 0, 0, 0, 0, 0])
    assert decode(c.exchange(sample_frame(0, single=False))) == code_for(1.0, 3.3)
    assert decode(c.exchange(sample_frame(1, single=False))) == 0


def test_leading_zeros_before_the_start_bit_shift_the_answer() -> None:
    c = chip_with([0.0, 0.0, 0.0, 1.65, 0, 0, 0, 0])
    # start bit as the first bit of byte 1, config in byte 1: the 10 bits land one byte earlier
    rx = c.exchange(bytes([0x00, 0x01, 0xB0, 0x00, 0x00]))
    assert ((rx[2] & 0x03) << 8 | rx[3]) == 512


def test_clocking_past_b0_gives_the_word_again_lsb_first() -> None:
    c = chip_with([0.0] * 7 + [2.47])
    code = code_for(2.47, 3.3)
    rx = c.exchange(sample_frame(7) + b"\x00\x00")
    bits = "".join(f"{b:08b}" for b in rx)
    msb = bits[14:24]
    lsb_tail = bits[24:33]
    assert int(msb, 2) == code
    assert lsb_tail == msb[:-1][::-1]  # B1..B9
    assert set(bits[33:]) <= {"0"}


def test_a_frame_without_a_start_bit_returns_zeros_and_converts_nothing() -> None:
    c = chip_with([3.0] * 8)
    assert c.exchange(b"\x00\x00\x00") == b"\x00\x00\x00"
    assert c.conversions == 0
