"""The one-line meanings the panel shows, checked against values from the chip models and data sheets."""

from __future__ import annotations

import struct

from patchbay.chips.sht3x import raw_humidity, raw_temperature, word_with_crc
from patchbay.decode import describe_i2c, describe_spi


def test_a_chip_id_read_names_the_register_and_the_value() -> None:
    assert describe_i2c([(0x28, False, b"\x00", True), (0x28, True, b"\xa0", True)]) == "BNO055 @0x28 CHIP_ID = 0xA0"


def test_euler_angles_are_scaled_16_lsb_per_degree() -> None:
    data = struct.pack("<3h", 90 * 16, -2 * 16, 5 * 8)
    got = describe_i2c([(0x28, False, b"\x1a", True), (0x28, True, data, True)])
    assert got == "BNO055 @0x28 EUL_DATA heading 90.00°, roll -2.00°, pitch 2.50°"


def test_a_mode_write_names_the_mode() -> None:
    assert describe_i2c([(0x28, False, b"\x3d\x0c", True)]) == "BNO055 @0x28 OPR_MODE ← NDOF"


def test_calibration_status_splits_the_four_levels() -> None:
    got = describe_i2c([(0x28, False, b"\x35", True), (0x28, True, bytes([0b11100100]), True)])
    assert got.endswith("CALIB_STAT sys 3, gyr 2, acc 1, mag 0")


def test_a_refused_transfer_says_no_acknowledge() -> None:
    assert describe_i2c([(0x28, False, b"\x00", False), (0x28, True, b"", False)]) == "BNO055 @0x28: no acknowledge"


def test_an_sht3x_reading_is_converted_and_its_crc_checked() -> None:
    good = word_with_crc(raw_temperature(22.5)) + word_with_crc(raw_humidity(45.0))
    assert describe_i2c([(0x44, True, good, True)]) == "SHT3x @0x44 T 22.50 °C, RH 45.00 %, CRC ok"
    bad = bytes([good[0] ^ 0x04]) + good[1:]
    assert "CRC MISMATCH" in describe_i2c([(0x44, True, bad, True)])


def test_an_sht3x_command_is_named() -> None:
    assert describe_i2c([(0x44, False, b"\x2c\x06", True)]) == (
        "SHT3x @0x44 measure, high repeatability, clock stretching"
    )


def test_an_unmodelled_address_says_so() -> None:
    assert describe_i2c([(0x50, False, b"\x00", False)]) == "0x50 (nothing modelled): no acknowledge"


def test_the_mcp3008_frame_decodes_channel_and_code() -> None:
    assert describe_spi(0, bytes.fromhex("018000"), bytes.fromhex("000200"), True) == (
        "MCP3008 cs0 CH0 single-ended → 512 (1.65 V)"
    )
    assert describe_spi(0, bytes.fromhex("019000"), bytes.fromhex("0003ff"), True).startswith("MCP3008 cs0 CH1")
    assert describe_spi(0, b"\x00", b"", False) == "MCP3008 cs0: transfer refused"
