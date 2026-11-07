"""The virtio SPI controller device, device ID 45, one request queue.

Layouts from the virtio specification's SPI controller device section, as Linux carries them in
include/uapi/linux/virtio_spi.h:
  config   u8 cs_max_number, u8 cs_change_supported, u8 tx_nbits_supported, u8 rx_nbits_supported,
           le32 bits_per_word_mask, le32 mode_func_supported, le32 max_freq_hz, le32 max_word_delay_ns,
           le32 max_cs_setup_ns, le32 max_cs_hold_ns, le32 max_cs_inactive_ns                    (32 bytes)
  request  device-readable: struct spi_transfer_head (32 bytes) then tx_buf, if any;
           device-writable: rx_buf, if any, then struct spi_transfer_result { u8 result; }
  head     u8 chip_select_id, u8 bits_per_word, u8 cs_change, u8 tx_nbits, u8 rx_nbits, u8 reserved[3],
           le32 mode, le32 freq, le32 word_delay_ns, le32 cs_setup_ns, le32 cs_delay_hold_ns,
           le32 cs_change_delay_inactive_ns
A full-duplex transfer has tx_buf and rx_buf of equal length. Each request is one chip-select assertion here.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import TextIO

from .clock import Clock
from .faults import FaultPlan
from .spi_bus import MODE_CPHA, MODE_CPOL, MODE_CS_HIGH, MODE_LOOP, MODE_LSB_FIRST, SpiBus, SpiTransfer
from .vring import Chain, DeviceRing

DEVICE_ID = 45
TRANS_OK = 0
PARAM_ERR = 1
TRANS_ERR = 2
HEAD = struct.Struct("<BBBBB3xIIIIII")
CONFIG = struct.Struct("<BBBBIIIIIII")

MF_CPHA_0 = 1 << 0
MF_CPHA_1 = 1 << 1
MF_CPOL_0 = 1 << 2
MF_CPOL_1 = 1 << 3
MF_CS_HIGH = 1 << 4
MF_LSB_FIRST = 1 << 5
MF_LOOPBACK = 1 << 6


def bpw_mask(*bits: int) -> int:
    m = 0
    for b in bits:
        m |= 1 << (b - 1)
    return m


@dataclass(frozen=True)
class SpiConfig:
    cs_max_number: int = 2
    cs_change_supported: int = 1
    bits_per_word_mask: int = bpw_mask(8)
    mode_func_supported: int = MF_CPHA_0 | MF_CPHA_1 | MF_CPOL_0 | MF_CPOL_1 | MF_LSB_FIRST | MF_LOOPBACK
    max_freq_hz: int = 50_000_000

    def pack(self) -> bytes:
        return CONFIG.pack(
            self.cs_max_number,
            self.cs_change_supported,
            0,
            0,
            self.bits_per_word_mask,
            self.mode_func_supported,
            self.max_freq_hz,
            0,
            0,
            0,
            0,
        )

    def mode_ok(self, mode: int) -> bool:
        mf = self.mode_func_supported
        if mode & ~(MODE_CPHA | MODE_CPOL | MODE_CS_HIGH | MODE_LSB_FIRST | MODE_LOOP):
            return False
        checks = [
            (MF_CPHA_1 if mode & MODE_CPHA else MF_CPHA_0),
            (MF_CPOL_1 if mode & MODE_CPOL else MF_CPOL_0),
        ]
        if mode & MODE_CS_HIGH:
            checks.append(MF_CS_HIGH)
        if mode & MODE_LSB_FIRST:
            checks.append(MF_LSB_FIRST)
        if mode & MODE_LOOP:
            checks.append(MF_LOOPBACK)
        return all(mf & c for c in checks)


@dataclass
class SpiRequest:
    chain: Chain
    cs: int
    bits_per_word: int
    mode: int
    freq: int
    tx: bytes
    rx_len: int
    well_formed: bool


def parse_request(chain: Chain) -> SpiRequest | None:
    if chain.writable_len < 1:
        return None
    out = chain.read_all()
    rx_len = chain.writable_len - 1
    if len(out) < HEAD.size:
        return SpiRequest(chain, 0, 0, 0, 0, b"", rx_len, well_formed=False)
    cs, bpw, _cs_change, tx_nbits, rx_nbits, mode, freq, *_delays = HEAD.unpack_from(out)
    tx = out[HEAD.size :]
    well_formed = tx_nbits in (0, 1) and rx_nbits in (0, 1) and (not tx or not rx_len or len(tx) == rx_len)
    return SpiRequest(chain, cs, bpw, mode, freq, tx, rx_len, well_formed)


def answer(req: SpiRequest, result: int, rx: bytes = b"") -> int:
    body = rx[: req.rx_len] if result == TRANS_OK else b""
    body += bytes(req.rx_len - len(body))
    payload = body + bytes([result])
    req.chain.write_all(payload)
    return len(payload)


@dataclass
class Pending:
    req: SpiRequest
    not_before: float


@dataclass
class VirtioSpi:
    bus: SpiBus
    faults: FaultPlan
    clock: Clock
    spi_config: SpiConfig = field(default_factory=SpiConfig)
    device_id: int = DEVICE_ID
    device_features: int = 0
    num_queues: int = 1
    pending: list[Pending] = field(default_factory=list)
    completed: int = 0
    failed: int = 0
    trace: TextIO | None = None

    @property
    def config(self) -> bytes:
        return self.spi_config.pack()

    def reset(self) -> None:
        self.pending.clear()

    def check(self, req: SpiRequest) -> int:
        c = self.spi_config
        if not req.well_formed or req.cs >= c.cs_max_number:
            return PARAM_ERR
        if req.bits_per_word == 0 or req.bits_per_word > 32:
            return PARAM_ERR
        if c.bits_per_word_mask and not c.bits_per_word_mask & (1 << (req.bits_per_word - 1)):
            return PARAM_ERR  # a mask of 0 means no limitation (virtio_spi.h, bits_per_word_mask)
        if c.max_freq_hz and req.freq > c.max_freq_hz:
            return PARAM_ERR
        if not c.mode_ok(req.mode):
            return PARAM_ERR
        return TRANS_OK

    def kick(self, ring: DeviceRing) -> list[tuple[int, int]]:
        done: list[tuple[int, int]] = []
        while True:
            chain = ring.pop()
            if chain is None:
                break
            req = parse_request(chain)
            if req is None:
                done.append((chain.head, 0))
                continue
            delay = self.faults.take("spi", "delay")
            wait = delay.delay_ms / 1000.0 if delay else 0.0
            self.pending.append(Pending(req, self.clock.now() + wait))
        return done + self.poll()

    def next_deadline(self) -> float | None:
        if not self.pending or self.faults.active("spi", "stuck"):
            return None
        return self.pending[0].not_before

    def poll(self) -> list[tuple[int, int]]:
        done: list[tuple[int, int]] = []
        now = self.clock.now()
        while self.pending and not self.faults.active("spi", "stuck") and self.pending[0].not_before <= now:
            done.append(self._run(self.pending.pop(0).req))
        return done

    def _run(self, req: SpiRequest) -> tuple[int, int]:
        result = self.check(req)
        if result == TRANS_OK and self.faults.take("spi", "error", req.cs):
            result = TRANS_ERR
        if result != TRANS_OK:
            self.failed += 1
            self._trace(req, "param-err" if result == PARAM_ERR else "trans-err")
            return (req.chain.head, answer(req, result))
        rx = self.bus.transfer(SpiTransfer(req.cs, req.tx, req.rx_len, req.mode, req.freq, req.bits_per_word))
        self.completed += 1
        self._trace(req, "ok " + rx.hex())
        return (req.chain.head, answer(req, TRANS_OK, rx))

    def _trace(self, req: SpiRequest, state: str) -> None:
        if self.trace is not None:
            print(f"spi  cs{req.cs} tx {req.tx.hex() or '-'} rx{req.rx_len} {state}", file=self.trace, flush=True)
