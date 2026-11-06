"""The device's half of a split virtqueue (virtio 1.2, section 2.7).

Layouts, all little-endian:
  descriptor  le64 addr, le32 len, le16 flags, le16 next          (16 bytes each)
  available   le16 flags, le16 idx, le16 ring[size], le16 used_event
  used        le16 flags, le16 idx, {le32 id, le32 len}[size], le16 avail_event
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .memory import GuestMemory, MemoryFault

DESC_F_NEXT = 1
DESC_F_WRITE = 2
DESC_F_INDIRECT = 4
AVAIL_F_NO_INTERRUPT = 1

DESC_SIZE = 16


class RingError(Exception):
    """The driver broke a rule of the split ring. The device stops using the queue (virtio 1.2, 2.1.2)."""


@dataclass
class Chain:
    """One request: the buffers the device may read, then the buffers it may write, in descriptor order."""

    head: int
    readable: list[memoryview] = field(default_factory=list)
    writable: list[memoryview] = field(default_factory=list)

    def read_all(self) -> bytes:
        return b"".join(bytes(b) for b in self.readable)

    @property
    def writable_len(self) -> int:
        return sum(len(b) for b in self.writable)

    def write_all(self, data: bytes) -> int:
        """Scatter data over the writable buffers in order. Returns the byte count written."""
        if len(data) > self.writable_len:
            raise ValueError("more data than writable space")
        pos = 0
        for b in self.writable:
            n = min(len(b), len(data) - pos)
            if n <= 0:
                break
            b[:n] = data[pos : pos + n]
            pos += n
        return pos


class DeviceRing:
    def __init__(
        self,
        mem: GuestMemory,
        size: int,
        desc_gpa: int,
        avail_gpa: int,
        used_gpa: int,
        last_avail: int = 0,
        indirect: bool = False,
    ) -> None:
        if size <= 0 or size > 32768 or size & (size - 1):
            raise RingError(f"queue size {size} is not a power of 2 up to 32768")
        self.indirect = indirect
        self.mem = mem
        self.size = size
        self.desc_gpa = desc_gpa
        self.avail_gpa = avail_gpa
        self.used_gpa = used_gpa
        self.last_avail = last_avail & 0xFFFF
        self.used_idx = struct.unpack_from("<H", self.mem.view(used_gpa + 2, 2))[0]

    def avail_idx(self) -> int:
        return int(struct.unpack_from("<H", self.mem.view(self.avail_gpa + 2, 2))[0])

    def pending(self) -> int:
        return (self.avail_idx() - self.last_avail) & 0xFFFF

    def pop(self) -> Chain | None:
        """The next request the driver made available, or None. Raises RingError on a malformed chain."""
        n = self.pending()
        if n == 0:
            return None
        if n > self.size:
            raise RingError(f"driver claims {n} new entries in a ring of {self.size}")
        slot = self.last_avail % self.size
        head = struct.unpack_from("<H", self.mem.view(self.avail_gpa + 4 + 2 * slot, 2))[0]
        chain = self._walk(head)
        self.last_avail = (self.last_avail + 1) & 0xFFFF
        return chain

    def _walk(self, head: int) -> Chain:
        if head >= self.size:
            raise RingError(f"head index {head} outside a ring of {self.size}")
        chain = Chain(head=head)
        addr, length, flags, nxt = self._desc(self.desc_gpa, head)
        if flags & DESC_F_INDIRECT:
            # virtio 1.2, 2.7.5.3: the ring descriptor names a table; it must not chain further itself.
            if not self.indirect:
                raise RingError("indirect descriptor, but VIRTIO_RING_F_INDIRECT_DESC was not negotiated")
            if flags & DESC_F_NEXT:
                raise RingError("descriptor has both INDIRECT and NEXT")
            if length == 0 or length % DESC_SIZE:
                raise RingError(f"indirect table of {length} bytes is not a whole number of descriptors")
            self._follow(chain, addr, length // DESC_SIZE, 0, in_table=True)
        else:
            self._follow(chain, self.desc_gpa, self.size, head, in_table=False)
        return chain

    def _desc(self, table: int, idx: int) -> tuple[int, int, int, int]:
        try:
            return struct.unpack_from("<QIHH", self.mem.view(table + DESC_SIZE * idx, DESC_SIZE))  # type: ignore[return-value]
        except MemoryFault as e:
            raise RingError(str(e)) from e

    def _follow(self, chain: Chain, table: int, count: int, idx: int, in_table: bool) -> None:
        for _ in range(count):
            addr, length, flags, nxt = self._desc(table, idx)
            if flags & DESC_F_INDIRECT:
                raise RingError("indirect descriptor inside an indirect table" if in_table else "INDIRECT in a chain")
            try:
                buf = self.mem.view(addr, length)
            except MemoryFault as e:
                raise RingError(str(e)) from e
            if flags & DESC_F_WRITE:
                chain.writable.append(buf)
            else:
                if chain.writable:
                    raise RingError("device-readable descriptor after a device-writable one")
                chain.readable.append(buf)
            if not flags & DESC_F_NEXT:
                return
            if nxt >= count:
                raise RingError(f"next index {nxt} outside a table of {count}")
            idx = nxt
        raise RingError("descriptor chain longer than its table: a loop")

    def push(self, head: int, written: int) -> None:
        """Return a request to the driver, with the byte count the device wrote into it."""
        slot = self.used_idx % self.size
        struct.pack_into("<II", self.mem.view(self.used_gpa + 4 + 8 * slot, 8), 0, head, written)
        self.used_idx = (self.used_idx + 1) & 0xFFFF
        struct.pack_into("<H", self.mem.view(self.used_gpa + 2, 2), 0, self.used_idx)

    def wants_interrupt(self) -> bool:
        flags = struct.unpack_from("<H", self.mem.view(self.avail_gpa, 2))[0]
        return not flags & AVAIL_F_NO_INTERRUPT
