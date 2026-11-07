"""The device half of the split ring: what it accepts, what it refuses, and what it writes back."""

from __future__ import annotations

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patchbay.memory import GuestMemory, MemoryFault, Region
from patchbay.vring import DESC_F_NEXT, DESC_F_WRITE, DeviceRing, RingError
from tests.ringkit import Arena, DriverRing


def test_a_chain_comes_back_as_readable_then_writable_buffers_in_order() -> None:
    a = Arena()
    d = DriverRing(a)
    head, ins = d.add([b"\x50\x00\x00\x00\x01\x00\x00\x00", b"\x1a"], [1])
    ring = d.device()
    chain = ring.pop()
    assert chain is not None and chain.head == head
    assert chain.read_all() == b"\x50\x00\x00\x00\x01\x00\x00\x00\x1a"
    assert chain.writable_len == 1
    assert ring.pop() is None


def test_push_writes_the_used_element_and_bumps_the_index() -> None:
    a = Arena()
    d = DriverRing(a)
    head, _ = d.add([b"x"], [4])
    ring = d.device()
    ring.pop()
    ring.push(head, 3)
    assert d.reap() == [(head, 3)]


def test_the_device_starts_from_the_used_index_already_in_memory() -> None:
    a = Arena()
    d = DriverRing(a)
    a.write(d.used + 2, struct.pack("<H", 7))
    assert d.device().used_idx == 7


@pytest.mark.parametrize("size", [0, 3, 6, 65536])
def test_queue_sizes_that_are_not_powers_of_two_up_to_32768_are_refused(size: int) -> None:
    a = Arena()
    with pytest.raises(RingError):
        DeviceRing(a.mem, size, a.base, a.base + 0x1000, a.base + 0x2000)


def test_a_readable_buffer_after_a_writable_one_is_refused() -> None:
    a = Arena()
    d = DriverRing(a)
    x = a.put(b"abcd")
    d.write_desc(d.desc, 0, x, 4, DESC_F_WRITE | DESC_F_NEXT, 1)
    d.write_desc(d.desc, 1, x, 4, 0, 0)
    d.publish_head(0)
    with pytest.raises(RingError, match="readable descriptor after"):
        d.device().pop()


def test_a_descriptor_loop_is_refused() -> None:
    a = Arena()
    d = DriverRing(a, size=4)
    x = a.put(b"abcd")
    d.write_desc(d.desc, 0, x, 4, DESC_F_NEXT, 1)
    d.write_desc(d.desc, 1, x, 4, DESC_F_NEXT, 0)
    d.publish_head(0)
    with pytest.raises(RingError, match="loop"):
        d.device().pop()


def test_next_and_head_indices_outside_the_ring_are_refused() -> None:
    a = Arena()
    d = DriverRing(a, size=4)
    x = a.put(b"abcd")
    d.write_desc(d.desc, 0, x, 4, DESC_F_NEXT, 9)
    d.publish_head(0)
    with pytest.raises(RingError, match="outside"):
        d.device().pop()
    d2 = DriverRing(a, size=4)
    d2.publish_head(5)
    with pytest.raises(RingError, match="head index"):
        d2.device().pop()


def test_a_buffer_outside_shared_memory_is_refused() -> None:
    a = Arena()
    d = DriverRing(a)
    d.write_desc(d.desc, 0, 0xDEAD0000, 4, 0, 0)
    d.publish_head(0)
    with pytest.raises(RingError, match="not in shared memory"):
        d.device().pop()


def test_more_new_entries_than_the_ring_holds_is_refused() -> None:
    a = Arena()
    d = DriverRing(a, size=4)
    a.write(d.avail + 2, struct.pack("<H", 9))
    with pytest.raises(RingError, match="claims 9"):
        d.device().pop()


def test_an_indirect_descriptor_needs_the_feature() -> None:
    a = Arena()
    d = DriverRing(a)
    d.add_indirect([b"head"], [2])
    with pytest.raises(RingError, match="INDIRECT_DESC"):
        d.device(indirect=False).pop()


def test_an_indirect_table_is_followed_when_negotiated() -> None:
    a = Arena()
    d = DriverRing(a)
    head, _ = d.add_indirect([b"ab", b"cd"], [3])
    chain = d.device(indirect=True).pop()
    assert chain is not None and chain.head == head
    assert chain.read_all() == b"abcd" and chain.writable_len == 3


def test_no_interrupt_flag_is_read_from_the_available_ring() -> None:
    a = Arena()
    d = DriverRing(a)
    ring = d.device()
    assert ring.wants_interrupt()
    a.write(d.avail, struct.pack("<H", 1))
    assert not ring.wants_interrupt()


def test_guest_memory_refuses_a_range_across_two_regions() -> None:
    b1, b2 = bytearray(16), bytearray(16)
    mem = GuestMemory([Region(0, 16, 1000, memoryview(b1)), Region(16, 16, 2000, memoryview(b2))])
    assert len(mem.view(8, 8)) == 8
    with pytest.raises(MemoryFault):
        mem.view(8, 16)
    assert mem.uva_to_gpa(2004) == 20
    with pytest.raises(MemoryFault):
        mem.uva_to_gpa(5000)


@given(
    payload=st.binary(min_size=1, max_size=64),
    cuts=st.lists(st.integers(min_value=1, max_value=63), max_size=4),
    wlen=st.integers(min_value=1, max_value=48),
    wcuts=st.lists(st.integers(min_value=1, max_value=47), max_size=3),
)
def test_descriptor_boundaries_do_not_change_what_the_device_reads_or_writes(
    payload: bytes, cuts: list[int], wlen: int, wcuts: list[int]
) -> None:
    a = Arena()
    d = DriverRing(a, size=16)
    pts = sorted({c for c in cuts if c < len(payload)})
    pieces = [payload[i:j] for i, j in zip([0, *pts], [*pts, len(payload)], strict=True)]
    wpts = sorted({c for c in wcuts if c < wlen})
    wl = [j - i for i, j in zip([0, *wpts], [*wpts, wlen], strict=True)]
    _, ins = d.add(pieces, wl)
    chain = d.device().pop()
    assert chain is not None
    assert chain.read_all() == payload
    data = bytes(range(wlen))
    assert chain.write_all(data) == wlen
    assert b"".join(a.read(g, n) for g, n in zip(ins, wl, strict=True)) == data
