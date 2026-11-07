"""ctypes bindings to the host build of driver/core (driver/build/host/libpbcore.so).

The C code and the Python device then share one block of memory, at a guest-physical address chosen here, so each
side reads exactly what the other wrote.
"""

from __future__ import annotations

import ctypes as C
import os
import subprocess
from pathlib import Path

from patchbay.memory import GuestMemory, Region

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "driver" / "build" / "host" / "libpbcore.so"
BASE = 0x4000_0000


class Buf(C.Structure):
    _fields_ = [("addr", C.c_uint64), ("len", C.c_uint32)]


class I2cSeg(C.Structure):
    _fields_ = [
        ("addr", C.c_uint16),
        ("read", C.c_uint8),
        ("len", C.c_uint32),
        ("wdata", C.c_void_p),
        ("rdata", C.c_void_p),
    ]


class I2cReq(C.Structure):
    _fields_ = [("out", Buf), ("inb", Buf), ("out_off", C.c_size_t), ("in_off", C.c_size_t)]


class I2cPlan(C.Structure):
    _fields_ = [("n", C.c_uint), ("req", I2cReq * 8)]


class SpiReq(C.Structure):
    _fields_ = [("out", Buf), ("inb", Buf), ("out_off", C.c_size_t), ("in_off", C.c_size_t)]


class SpiConfig(C.Structure):
    _fields_ = [
        ("cs_max_number", C.c_uint8),
        ("cs_change_supported", C.c_uint8),
        ("tx_nbits_supported", C.c_uint8),
        ("rx_nbits_supported", C.c_uint8),
        ("bits_per_word_mask", C.c_uint32),
        ("mode_func_supported", C.c_uint32),
        ("max_freq_hz", C.c_uint32),
        ("max_word_delay_ns", C.c_uint32),
        ("max_cs_setup_ns", C.c_uint32),
        ("max_cs_hold_ns", C.c_uint32),
        ("max_cs_inactive_ns", C.c_uint32),
    ]


def load() -> C.CDLL:
    """The host build of the core. PB_CORE_LIB names another build, such as a deliberately broken one, to check
    that these tests fail when the code is wrong."""
    override = os.environ.get("PB_CORE_LIB")
    if override:
        lib = C.CDLL(override)
    else:
        subprocess.run(["make", "-s", "-C", str(ROOT / "driver"), "build/host/libpbcore.so"], check=True)
        lib = C.CDLL(str(LIB))
    lib.pb_vq_layout.restype = C.c_size_t
    lib.pb_vq_layout.argtypes = [C.c_uint16, C.c_void_p, C.c_void_p]
    lib.pb_vq_init.argtypes = [C.c_void_p, C.c_void_p, C.c_uint64, C.c_uint16]
    lib.pb_vq_add.argtypes = [C.c_void_p, C.POINTER(Buf), C.c_uint, C.POINTER(Buf), C.c_uint, C.POINTER(C.c_uint16)]
    lib.pb_vq_publish.argtypes = [C.c_void_p]
    lib.pb_vq_get_used.argtypes = [C.c_void_p, C.POINTER(C.c_uint16), C.POINTER(C.c_uint32)]
    lib.pb_vq_reset.argtypes = [C.c_void_p]
    lib.pb_i2c_plan.argtypes = [
        C.POINTER(I2cSeg),
        C.c_uint,
        C.c_int,
        C.c_void_p,
        C.c_uint64,
        C.c_size_t,
        C.POINTER(I2cPlan),
    ]
    lib.pb_i2c_collect.argtypes = [C.POINTER(I2cSeg), C.POINTER(I2cPlan), C.c_void_p, C.POINTER(C.c_uint32)]
    lib.pb_spi_plan.argtypes = [
        C.c_uint8,
        C.c_uint8,
        C.c_uint32,
        C.c_uint32,
        C.c_void_p,
        C.c_uint32,
        C.c_uint32,
        C.c_void_p,
        C.c_uint64,
        C.c_size_t,
        C.POINTER(SpiReq),
    ]
    lib.pb_spi_collect.restype = C.c_uint
    lib.pb_spi_collect.argtypes = [C.POINTER(SpiReq), C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32]
    lib.pb_spi_mode_from_qnx.restype = C.c_uint32
    lib.pb_spi_mode_from_qnx.argtypes = [C.c_uint32, C.POINTER(C.c_uint8)]
    lib.pb_spi_check.argtypes = [C.POINTER(SpiConfig), C.c_uint32, C.c_uint8, C.c_uint32]
    for name in ("pb_test_sizeof_vq", "pb_test_sizeof_i2c_plan", "pb_test_sizeof_spi_req", "pb_test_sizeof_i2c_seg"):
        getattr(lib, name).restype = C.c_size_t
    return lib


class Shared:
    """size bytes, 4 KiB aligned, visible to C by pointer and to Python as guest memory at BASE."""

    def __init__(self, size: int = 1 << 20) -> None:
        self.raw = C.create_string_buffer(size + 4096)
        self.ptr = (C.addressof(self.raw) + 4095) & ~4095
        self.off = self.ptr - C.addressof(self.raw)
        self.size = size
        view = memoryview(self.raw).cast("B")[self.off : self.off + size]
        self.mem = GuestMemory([Region(BASE, size, 0x7F00_0000_0000, view)])
        self.top = 0

    def alloc(self, n: int, align: int = 16) -> tuple[int, int]:
        """(cpu pointer, guest-physical address) of n fresh bytes."""
        self.top = (self.top + align - 1) & ~(align - 1)
        p, g = self.ptr + self.top, BASE + self.top
        self.top += n
        assert self.top <= self.size
        return p, g


class CRing:
    """The driver's ring, the C one, in shared memory."""

    def __init__(self, lib: C.CDLL, shared: Shared, size: int) -> None:
        self.lib = lib
        self.vq = C.create_string_buffer(lib.pb_test_sizeof_vq())
        nbytes = lib.pb_vq_layout(size, None, None)
        self.ptr, self.gpa = shared.alloc(nbytes, 4096)
        avail, used = C.c_size_t(), C.c_size_t()
        lib.pb_vq_layout(size, C.byref(avail), C.byref(used))
        self.avail_gpa = self.gpa + avail.value
        self.used_gpa = self.gpa + used.value
        rc = lib.pb_vq_init(self.vq, self.ptr, self.gpa, size)
        assert rc == 0
        self.size = size

    def add(self, out: list[tuple[int, int]], ins: list[tuple[int, int]]) -> int:
        o = (Buf * max(1, len(out)))(*[Buf(a, n) for a, n in out])
        i = (Buf * max(1, len(ins)))(*[Buf(a, n) for a, n in ins])
        head = C.c_uint16()
        rc = self.lib.pb_vq_add(self.vq, o, len(out), i, len(ins), C.byref(head))
        if rc != 0:
            raise OSError(-rc, "pb_vq_add")
        return head.value

    def publish(self) -> None:
        self.lib.pb_vq_publish(self.vq)

    def get_used(self) -> tuple[int, int, int]:
        h, n = C.c_uint16(), C.c_uint32()
        rc = self.lib.pb_vq_get_used(self.vq, C.byref(h), C.byref(n))
        return rc, h.value, n.value
