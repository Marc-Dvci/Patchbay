"""Guest memory as the front end shares it: a list of regions, each addressed three ways.

QEMU sends each region's guest-physical address, its address in QEMU's own process, and a file descriptor with an offset
(vhost-user, "Memory region description"). Descriptors carry guest-physical addresses; VHOST_USER_SET_VRING_ADDR carries
QEMU process addresses. Both translate here.
"""

from __future__ import annotations

from dataclasses import dataclass


class MemoryFault(Exception):
    """An address range that no shared region covers in full."""


@dataclass(frozen=True)
class Region:
    gpa: int
    size: int
    uva: int
    buf: memoryview  # the region's bytes, buf[0] is at gpa

    def __post_init__(self) -> None:
        if self.size <= 0 or len(self.buf) < self.size:
            raise ValueError("region buffer shorter than its size")


class GuestMemory:
    def __init__(self, regions: list[Region] | None = None) -> None:
        self.regions: list[Region] = list(regions or [])

    def view(self, gpa: int, length: int) -> memoryview:
        """Bytes [gpa, gpa + length) of guest memory, without copying. A range across two regions is refused."""
        if length < 0 or gpa < 0:
            raise MemoryFault(f"bad range {gpa:#x}+{length}")
        for r in self.regions:
            if r.gpa <= gpa and gpa + length <= r.gpa + r.size:
                off = gpa - r.gpa
                return r.buf[off : off + length]
        raise MemoryFault(f"guest range {gpa:#x}+{length} is not in shared memory")

    def uva_to_gpa(self, uva: int) -> int:
        for r in self.regions:
            if r.uva <= uva < r.uva + r.size:
                return r.gpa + (uva - r.uva)
        raise MemoryFault(f"front-end address {uva:#x} is not in shared memory")
