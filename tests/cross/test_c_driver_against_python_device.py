"""The driver's C core against the back end's Python device: two independent implementations of the same spec.

Each test posts requests with the C code (the code that runs in the QNX drivers), lets the Python device parse and
complete them in shared memory, and reaps the completions with the C code again.
"""

from __future__ import annotations

import ctypes as C
import random
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from patchbay.chips.bno055 import Bno055
from patchbay.chips.mcp3008 import Mcp3008, code_for
from patchbay.clock import FakeClock
from patchbay.faults import Fault, FaultPlan
from patchbay.i2c_bus import I2cBus
from patchbay.spi_bus import SpiBus
from patchbay.virtio_i2c import VirtioI2c
from patchbay.virtio_spi import SpiConfig as PySpiConfig
from patchbay.virtio_spi import VirtioSpi
from patchbay.vring import DeviceRing
from tests.cross.cbind import CRing, I2cPlan, I2cSeg, Shared, SpiConfig, SpiReq, load

pytestmark = pytest.mark.cross

LIB = load()


def device_ring(shared: Shared, ring: CRing) -> DeviceRing:
    return DeviceRing(shared.mem, ring.size, ring.gpa, ring.avail_gpa, ring.used_gpa)


def test_the_ctypes_mirrors_have_the_c_structures_sizes() -> None:
    assert C.sizeof(I2cPlan) == LIB.pb_test_sizeof_i2c_plan()
    assert C.sizeof(SpiReq) == LIB.pb_test_sizeof_spi_req()
    assert C.sizeof(I2cSeg) == LIB.pb_test_sizeof_i2c_seg()


@settings(max_examples=60)
@given(
    size=st.sampled_from([4, 8, 16, 64]),
    requests=st.lists(
        st.tuples(
            st.lists(st.binary(min_size=1, max_size=40), min_size=0, max_size=2),
            st.lists(st.integers(1, 40), min_size=0, max_size=2),
        ).filter(lambda r: len(r[0]) + len(r[1]) > 0),
        min_size=1,
        max_size=30,
    ),
    seed=st.integers(0, 2**32 - 1),
)
def test_any_requests_cross_the_ring_intact_and_every_descriptor_comes_back(
    size: int, requests: list[tuple[list[bytes], list[int]]], seed: int
) -> None:
    rnd = random.Random(seed)
    shared = Shared()
    ring = CRing(LIB, shared, size)
    dev = device_ring(shared, ring)
    sent: dict[int, tuple[bytes, int]] = {}
    queue = list(requests)
    held: list[int] = []
    while queue or held or sent:
        # post as many as fit
        while queue:
            outs, ins = queue[0]
            out_bufs = []
            for b in outs:
                p, g = shared.alloc(len(b), 8)
                C.memmove(p, b, len(b))
                out_bufs.append((g, len(b)))
            in_bufs = [(shared.alloc(n, 8)[1], n) for n in ins]
            try:
                head = ring.add(out_bufs, in_bufs)
            except OSError:
                break  # ring full: complete some first
            queue.pop(0)
            sent[head] = (b"".join(outs), sum(ins))
        ring.publish()
        while (chain := dev.pop()) is not None:
            assert chain.read_all() == sent[chain.head][0]
            assert chain.writable_len == sent[chain.head][1]
            held.append(chain.head)
        rnd.shuffle(held)  # the device completes in any order
        for head in held[: rnd.randint(1, len(held))] if held else []:
            dev.push(head, sent[head][1])
            held.remove(head)
        while True:
            rc, head, n = ring.get_used()
            if rc == 0:
                break
            assert rc == 1 and head in sent and n == sent[head][1]
            del sent[head]
    # everything returned: the full ring is available again, as single descriptors
    p, g = shared.alloc(1)
    for _ in range(size):
        ring.add([(g, 1)], [])


def i2c_rig(shared: Shared) -> tuple[FakeClock, FaultPlan, VirtioI2c]:
    clock = FakeClock(10.0)
    plan = FaultPlan()
    bus = I2cBus(plan)
    bus.attach(Bno055(clock))
    return clock, plan, VirtioI2c(bus, plan, clock)


def c_i2c_transaction(
    shared: Shared, ring: CRing, dev: VirtioI2c, segs: list[tuple[int, bool, bytes | int]]
) -> tuple[int, list[bytes]]:
    """Plan segments with pb_i2c_plan, post them, let the Python device run them, collect with pb_i2c_collect."""
    arena_p, arena_g = shared.alloc(16384, 64)
    keep = []
    cs = (I2cSeg * len(segs))()
    for i, (addr, read, x) in enumerate(segs):
        if read:
            assert isinstance(x, int)
            buf = C.create_string_buffer(max(1, x))
            cs[i] = I2cSeg(addr, 1, x, None, C.cast(buf, C.c_void_p))
        else:
            assert isinstance(x, bytes)
            buf = C.create_string_buffer(x, max(1, len(x)))
            cs[i] = I2cSeg(addr, 0, len(x), C.cast(buf, C.c_void_p), None)
        keep.append(buf)
    plan = I2cPlan()
    assert LIB.pb_i2c_plan(cs, len(segs), 1, arena_p, arena_g, 16384, C.byref(plan)) == 0
    heads = [
        ring.add([(plan.req[i].out.addr, plan.req[i].out.len)], [(plan.req[i].inb.addr, plan.req[i].inb.len)])
        for i in range(len(segs))
    ]
    ring.publish()
    dring = device_ring(shared, ring)
    for head, n in dev.kick(dring):
        dring.push(head, n)
    used = (C.c_uint32 * len(segs))()
    for _ in segs:
        rc, head, n = ring.get_used()
        assert rc == 1
        used[heads.index(head)] = n
    failed = LIB.pb_i2c_collect(cs, C.byref(plan), arena_p, used)
    reads = [keep[i].raw[:x] for i, (_, r, x) in enumerate(segs) if r and isinstance(x, int)]
    return failed, reads


def test_the_c_framing_reads_the_bno055_chip_id_through_the_python_device() -> None:
    shared = Shared()
    ring = CRing(LIB, shared, 4)
    _, _, dev = i2c_rig(shared)
    failed, reads = c_i2c_transaction(shared, ring, dev, [(0x28, False, b"\x00"), (0x28, True, 4)])
    assert failed == -1
    assert reads == [bytes([0xA0, 0xFB, 0x32, 0x0F])]
    assert dev.bus.log == [(0x28, False, 1, True), (0x28, True, 4, True)]


def test_a_nak_reported_by_the_device_is_found_by_the_c_collector_at_the_first_message() -> None:
    shared = Shared()
    ring = CRing(LIB, shared, 4)
    _, plan, dev = i2c_rig(shared)
    plan.add(Fault("nak", "i2c", target=0x28))
    failed, _ = c_i2c_transaction(shared, ring, dev, [(0x28, False, b"\x00"), (0x28, True, 1)])
    assert failed == 0


def test_the_c_framing_writes_a_register_the_model_then_holds() -> None:
    shared = Shared()
    ring = CRing(LIB, shared, 4)
    _, _, dev = i2c_rig(shared)
    failed, _ = c_i2c_transaction(shared, ring, dev, [(0x28, False, bytes([0x3D, 0x0C]))])
    assert failed == -1
    chip = dev.bus.chips[0x28]
    assert isinstance(chip, Bno055) and chip.mode == 0x0C


@settings(max_examples=40)
@given(
    msgs=st.lists(
        st.one_of(
            st.tuples(st.integers(0x08, 0x77), st.just(False), st.binary(min_size=0, max_size=64)),
            st.tuples(st.integers(0x08, 0x77), st.just(True), st.integers(0, 64)),
        ),
        min_size=1,
        max_size=2,
    )
)
def test_any_c_planned_transaction_parses_in_python_to_the_same_messages(
    msgs: list[tuple[int, bool, bytes | int]],
) -> None:
    shared = Shared()
    ring = CRing(LIB, shared, 4)
    clock, plan, dev = i2c_rig(shared)
    seen: list[tuple[int, bool, bytes, int, bool]] = []
    real_run = dev._run

    def spy(group):  # type: ignore[no-untyped-def]
        for r in group:
            seen.append((r.addr, r.read, r.data, r.read_len, r.fail_next))
        return real_run(group)

    dev._run = spy  # type: ignore[method-assign]
    c_i2c_transaction(shared, ring, dev, msgs)
    want = []
    for k, (addr, read, x) in enumerate(msgs):
        last = k == len(msgs) - 1
        want.append((addr, read, b"" if read else x, x if read else 0, not last))
    assert seen == want


def spi_rig(cfg: PySpiConfig | None = None) -> VirtioSpi:
    clock = FakeClock()
    plan = FaultPlan()
    bus = SpiBus(cs_count=2, faults=plan)
    bus.attach(0, Mcp3008(clock, inputs=lambda t: [1.65, 0.5, 0, 0, 0, 0, 0, 3.3]))
    return VirtioSpi(bus, plan, clock, spi_config=cfg or PySpiConfig())


def c_spi_transfer(
    shared: Shared, ring: CRing, dev: VirtioSpi, tx: bytes, mode_qnx: int, freq: int
) -> tuple[int, bytes]:
    arena_p, arena_g = shared.alloc(4096, 64)
    bpw = C.c_uint8()
    mode = LIB.pb_spi_mode_from_qnx(mode_qnx, C.byref(bpw))
    req = SpiReq()
    txb = C.create_string_buffer(tx, len(tx))
    assert LIB.pb_spi_plan(0, bpw.value, mode, freq, txb, len(tx), len(tx), arena_p, arena_g, 4096, C.byref(req)) == 0
    ring.add([(req.out.addr, req.out.len)], [(req.inb.addr, req.inb.len)])
    ring.publish()
    dring = device_ring(shared, ring)
    for head, n in dev.kick(dring):
        dring.push(head, n)
    rc, head, n = ring.get_used()
    assert rc == 1
    rx = C.create_string_buffer(len(tx))
    result = LIB.pb_spi_collect(C.byref(req), arena_p, n, rx, len(tx))
    return result, rx.raw


@pytest.mark.parametrize("channel,volts", [(0, 1.65), (1, 0.5), (7, 3.3)])
def test_the_mcp3008_samples_frame_through_c_and_python_returns_the_code(channel: int, volts: float) -> None:
    shared = Shared()
    ring = CRing(LIB, shared, 4)
    dev = spi_rig()
    msb_first_8bit = (1 << 10) | 8
    result, rx = c_spi_transfer(shared, ring, dev, bytes([0x01, (0x08 | channel) << 4, 0]), msb_first_8bit, 1_000_000)
    assert result == 0
    assert ((rx[1] & 3) << 8 | rx[2]) == code_for(volts, 3.3)


def test_a_word_width_the_controller_refuses_comes_back_as_param_err() -> None:
    shared = Shared()
    ring = CRing(LIB, shared, 4)
    dev = spi_rig()
    result, _ = c_spi_transfer(shared, ring, dev, b"\x00\x00", (1 << 10) | 16, 1_000_000)
    assert result == 1


def test_a_zero_bits_per_word_mask_means_any_width_in_both_implementations() -> None:
    """Regression: the Python device once read mask 0 as "no width supported". Found by the property test below
    with qnx_mode=0x501, mask=0, mode_func_supported=6."""
    bpw = C.c_uint8()
    mode = LIB.pb_spi_mode_from_qnx(0x501, C.byref(bpw))
    ccfg = SpiConfig(2, 1, 0, 0, 0, 6, 50_000_000, 0, 0, 0, 0)
    assert LIB.pb_spi_check(C.byref(ccfg), mode, bpw.value, 0) == 0
    from patchbay.virtio_spi import SpiRequest
    from patchbay.vring import Chain

    dev = VirtioSpi(
        SpiBus(), FaultPlan(), FakeClock(), spi_config=PySpiConfig(bits_per_word_mask=0, mode_func_supported=6)
    )
    assert dev.check(SpiRequest(Chain(0), 0, bpw.value, mode, 0, b"", 1, well_formed=True)) == 0


@given(
    qnx_mode=st.integers(0, 0xFFFF),
    freq=st.integers(0, 80_000_000),
    mask=st.sampled_from([0, 1 << 7, (1 << 7) | (1 << 15)]),
    mf=st.integers(0, 0x7F),
)
def test_the_c_and_python_checks_agree_on_every_mode_width_and_clock(
    qnx_mode: int, freq: int, mask: int, mf: int
) -> None:
    bpw = C.c_uint8()
    mode = LIB.pb_spi_mode_from_qnx(qnx_mode, C.byref(bpw))
    ccfg = SpiConfig(2, 1, 0, 0, mask, mf, 50_000_000, 0, 0, 0, 0)
    c_ok = LIB.pb_spi_check(C.byref(ccfg), mode, bpw.value, freq) == 0
    pcfg = PySpiConfig(bits_per_word_mask=mask, mode_func_supported=mf, max_freq_hz=50_000_000)
    dev = VirtioSpi(SpiBus(), FaultPlan(), FakeClock(), spi_config=pcfg)
    from patchbay.virtio_spi import SpiRequest
    from patchbay.vring import Chain

    req = SpiRequest(Chain(0), 0, bpw.value, mode, freq, b"", 1, well_formed=True)
    py_ok = dev.check(req) == 0
    if bpw.value == 0 or bpw.value > 32:
        assert not c_ok
    else:
        assert c_ok == py_ok


def test_mode_translation_sets_each_virtio_bit_from_its_qnx_bit() -> None:
    bpw = C.c_uint8()
    assert LIB.pb_spi_mode_from_qnx((1 << 10) | 8, C.byref(bpw)) == 0 and bpw.value == 8
    assert LIB.pb_spi_mode_from_qnx((1 << 10) | (1 << 8), None) == 1  # CPHA
    assert LIB.pb_spi_mode_from_qnx((1 << 10) | (1 << 9), None) == 2  # CPOL
    assert LIB.pb_spi_mode_from_qnx((1 << 10) | (1 << 11), None) == 4  # CS_HIGH
    assert LIB.pb_spi_mode_from_qnx(0, None) == 8  # LSB first when BORDER_MSB is clear
    assert struct.calcsize("<BBBBB3xIIIIII") == 32  # spi_transfer_head
