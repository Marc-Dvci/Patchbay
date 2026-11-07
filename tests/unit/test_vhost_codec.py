"""vhost-user message decoding against the layouts in QEMU's docs/interop/vhost-user.rst."""

from __future__ import annotations

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patchbay import vhost_user as vu


def test_the_header_is_request_flags_size_little_endian() -> None:
    assert vu.HEADER.size == 12
    assert vu.decode_header(struct.pack("<III", 5, 0x1, 40)) == (5, 1, 40)


def test_a_header_with_another_version_is_refused() -> None:
    with pytest.raises(vu.ProtocolError, match="version"):
        vu.decode_header(struct.pack("<III", 1, 0x2, 0))


def test_an_oversized_payload_is_refused() -> None:
    with pytest.raises(vu.ProtocolError, match="limit"):
        vu.decode_header(struct.pack("<III", 1, 0x1, vu.MAX_PAYLOAD + 1))


def test_a_reply_sets_the_reply_flag() -> None:
    raw = vu.encode_reply(vu.Req.GET_FEATURES, b"\x00" * 8)
    req, flags, size = struct.unpack_from("<III", raw)
    assert (req, flags, size) == (1, vu.VERSION | vu.FLAG_REPLY, 8)


@given(st.lists(st.tuples(*[st.integers(0, 2**64 - 1)] * 4), min_size=1, max_size=8))
def test_a_memory_table_decodes_every_region(regions: list[tuple[int, int, int, int]]) -> None:
    raw = struct.pack("<II", len(regions), 0) + b"".join(struct.pack("<QQQQ", *r) for r in regions)
    got = vu.decode_mem_table(raw)
    assert [(m.gpa, m.size, m.uva, m.mmap_offset) for m in got] == regions


def test_a_memory_table_claiming_more_regions_than_it_carries_is_refused() -> None:
    with pytest.raises(vu.ProtocolError):
        vu.decode_mem_table(struct.pack("<II", 2, 0) + b"\x00" * 32)
    with pytest.raises(vu.ProtocolError):
        vu.decode_mem_table(struct.pack("<II", 9, 0) + b"\x00" * 32 * 9)


def test_vring_address_carries_index_flags_and_three_addresses_in_desc_used_avail_order() -> None:
    raw = struct.pack("<IIQQQQ", 0, 0, 0x1000, 0x2000, 0x3000, 0)
    assert vu.decode_vring_addr(raw) == (0, 0, 0x1000, 0x2000, 0x3000)


def test_vring_kick_payload_has_the_index_in_bits_0_7_and_nofd_in_bit_8() -> None:
    assert vu.VRING_IDX_MASK == 0xFF and vu.VRING_NOFD_MASK == 0x100


def test_a_config_reply_returns_the_requested_window_zero_padded() -> None:
    space = bytes(range(32))
    raw = vu.encode_config_reply(28, 8, 0, space)
    off, size, flags = struct.unpack_from("<III", raw)
    assert (off, size, flags) == (28, 8, 0)
    assert raw[12:] == bytes([28, 29, 30, 31, 0, 0, 0, 0])


def test_short_payloads_are_refused() -> None:
    for fn in (vu.decode_vring_state, vu.decode_vring_addr, vu.decode_config_request, vu.u64):
        with pytest.raises(vu.ProtocolError):
            fn(b"\x00")
