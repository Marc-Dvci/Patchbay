"""Test helpers: guest memory backed by a bytearray, and a minimal driver-side ring that writes what a driver writes.

The driver half here exists only to feed the device half in unit tests. The cross tests use the real driver half,
the C code in driver/core, instead.
"""

from __future__ import annotations

import struct

from patchbay.memory import GuestMemory, Region
from patchbay.vring import DESC_F_INDIRECT, DESC_F_NEXT, DESC_F_WRITE, DeviceRing

BASE = 0x100000


class Arena:
    """size bytes of guest memory at guest-physical BASE, with a bump allocator. buf may be shared memory (an mmap)."""

    def __init__(self, size: int = 1 << 18, base: int = BASE, buf: bytearray | memoryview | None = None) -> None:
        self.base = base
        self.buf = buf if buf is not None else bytearray(size)
        self.top = 0
        self.mem = GuestMemory([Region(base, size, 0x7F0000000000 + base, memoryview(self.buf))])

    def alloc(self, n: int, align: int = 16) -> int:
        self.top = (self.top + align - 1) & ~(align - 1)
        gpa = self.base + self.top
        self.top += n
        if self.top > len(self.buf):
            raise MemoryError("arena full")
        return gpa

    def put(self, data: bytes, align: int = 8) -> int:
        gpa = self.alloc(max(1, len(data)), align)
        self.write(gpa, data)
        return gpa

    def write(self, gpa: int, data: bytes) -> None:
        o = gpa - self.base
        self.buf[o : o + len(data)] = data

    def read(self, gpa: int, n: int) -> bytes:
        o = gpa - self.base
        return bytes(self.buf[o : o + n])


class DriverRing:
    """Writes descriptor chains, the available ring and its index, as a driver would. Size 4 to 256."""

    def __init__(self, arena: Arena, size: int = 8) -> None:
        self.arena = arena
        self.size = size
        self.desc = arena.alloc(16 * size, 16)
        self.avail = arena.alloc(6 + 2 * size, 2)
        self.used = arena.alloc(6 + 8 * size, 4)
        self.next_free = 0
        self.avail_idx = 0
        self.last_used = 0

    def device(self, indirect: bool = False) -> DeviceRing:
        return DeviceRing(self.arena.mem, self.size, self.desc, self.avail, self.used, indirect=indirect)

    def write_desc(self, table: int, i: int, addr: int, length: int, flags: int, nxt: int) -> None:
        self.arena.write(table + 16 * i, struct.pack("<QIHH", addr, length, flags, nxt))

    def add(self, out: list[bytes], in_lens: list[int]) -> tuple[int, list[int]]:
        """One chain; returns its head and the guest addresses of the writable buffers."""
        head = self.next_free
        bufs = [(self.arena.put(b), len(b), 0) for b in out]
        ins = [self.arena.alloc(n, 8) for n in in_lens]
        bufs += [(g, n, DESC_F_WRITE) for g, n in zip(ins, in_lens, strict=True)]
        for k, (gpa, n, fl) in enumerate(bufs):
            i = (head + k) % self.size
            last = k == len(bufs) - 1
            self.write_desc(self.desc, i, gpa, n, fl | (0 if last else DESC_F_NEXT), (i + 1) % self.size)
        self.next_free = (head + len(bufs)) % self.size
        self.publish_head(head)
        return head, ins

    def add_indirect(self, out: list[bytes], in_lens: list[int]) -> tuple[int, list[int]]:
        head = self.next_free
        bufs = [(self.arena.put(b), len(b), 0) for b in out]
        ins = [self.arena.alloc(n, 8) for n in in_lens]
        bufs += [(g, n, DESC_F_WRITE) for g, n in zip(ins, in_lens, strict=True)]
        table = self.arena.alloc(16 * len(bufs), 16)
        for k, (gpa, n, fl) in enumerate(bufs):
            last = k == len(bufs) - 1
            self.write_desc(table, k, gpa, n, fl | (0 if last else DESC_F_NEXT), k + 1)
        self.write_desc(self.desc, head, table, 16 * len(bufs), DESC_F_INDIRECT, 0)
        self.next_free = (head + 1) % self.size
        self.publish_head(head)
        return head, ins

    def publish_head(self, head: int) -> None:
        self.arena.write(self.avail + 4 + 2 * (self.avail_idx % self.size), struct.pack("<H", head))
        self.avail_idx = (self.avail_idx + 1) & 0xFFFF
        self.arena.write(self.avail + 2, struct.pack("<H", self.avail_idx))

    def reap(self) -> list[tuple[int, int]]:
        out = []
        idx = struct.unpack("<H", self.arena.read(self.used + 2, 2))[0]
        while self.last_used != idx:
            slot = self.last_used % self.size
            out.append(struct.unpack("<II", self.arena.read(self.used + 4 + 8 * slot, 8)))
            self.last_used = (self.last_used + 1) & 0xFFFF
        return out
