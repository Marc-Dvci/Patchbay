"""The virtio I2C adapter device (virtio 1.2, section 5.19), device ID 34, one request queue.

A request is one descriptor chain:
  device-readable   struct virtio_i2c_out_hdr { le16 addr; le16 padding; le32 flags; }  then write data, if any
  device-writable   read data, if any, then struct virtio_i2c_in_hdr { u8 status; }
flags bit 0 is VIRTIO_I2C_FLAGS_FAIL_NEXT, bit 1 is VIRTIO_I2C_FLAGS_M_RD. Requests chained by FAIL_NEXT form one
group, run as one combined transaction: if one fails, the rest of the group fails without reaching the bus.
addr carries the 7-bit address shifted left by one, as the Linux driver (drivers/i2c/busses/i2c-virtio.c) and the
rust-vmm vhost-device-i2c back end both encode it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import TextIO

from .clock import Clock
from .decode import describe_i2c
from .events import EventLog
from .faults import FaultPlan
from .i2c_bus import I2cBus, Segment
from .vring import Chain, DeviceRing

DEVICE_ID = 34
F_ZERO_LENGTH_REQUEST = 1 << 0
FLAGS_FAIL_NEXT = 1 << 0
FLAGS_M_RD = 1 << 1
MSG_OK = 0
MSG_ERR = 1
OUT_HDR = struct.Struct("<HHI")


class RequestError(Exception):
    """A request the device cannot even answer: it has no room for the status byte."""


@dataclass
class I2cRequest:
    chain: Chain
    addr: int
    read: bool
    fail_next: bool
    data: bytes
    read_len: int
    valid: bool = True


def parse_request(chain: Chain) -> I2cRequest:
    if chain.writable_len < 1:
        raise RequestError("request has no device-writable status byte")
    out = chain.read_all()
    read_len = chain.writable_len - 1
    if len(out) < OUT_HDR.size:
        return I2cRequest(chain, 0, False, False, b"", read_len, valid=False)
    raw_addr, _pad, flags = OUT_HDR.unpack_from(out)
    read = bool(flags & FLAGS_M_RD)
    data = out[OUT_HDR.size :]
    valid = not (read and data) and not (not read and read_len) and raw_addr <= 0xFF
    return I2cRequest(chain, raw_addr >> 1, read, bool(flags & FLAGS_FAIL_NEXT), data, read_len, valid)


def answer(req: I2cRequest, ok: bool, data: bytes = b"") -> int:
    """Write the reply into the chain: read data (zeros on failure), then the status byte. Returns the used length."""
    body = b""
    if req.read:
        body = (data if ok else b"")[: req.read_len]
        body += bytes(req.read_len - len(body))
    payload = body + bytes([MSG_OK if ok else MSG_ERR])
    req.chain.write_all(payload)
    return len(payload)


@dataclass
class Pending:
    group: list[I2cRequest]
    not_before: float


@dataclass
class VirtioI2c:
    bus: I2cBus
    faults: FaultPlan
    clock: Clock
    device_id: int = DEVICE_ID
    device_features: int = F_ZERO_LENGTH_REQUEST
    num_queues: int = 1
    config: bytes = b""
    partial: list[I2cRequest] = field(default_factory=list)
    pending: list[Pending] = field(default_factory=list)
    completed: int = 0
    failed: int = 0
    trace: TextIO | None = None
    events: EventLog | None = None

    def reset(self) -> None:
        """The front end stopped the queue: requests in flight are dropped, as a reset device would."""
        self.partial.clear()
        self.pending.clear()

    def kick(self, ring: DeviceRing) -> list[tuple[int, int]]:
        """Take what the driver made available, and complete what is due. Returns (head, used length) pairs."""
        done: list[tuple[int, int]] = []
        while True:
            chain = ring.pop()
            if chain is None:
                break
            try:
                req = parse_request(chain)
            except RequestError:
                done.append((chain.head, 0))
                continue
            self.partial.append(req)
            if not req.fail_next:
                group, self.partial = self.partial, []
                delay = self.faults.take("i2c", "delay")
                wait = delay.delay_ms / 1000.0 if delay else 0.0
                self.pending.append(Pending(group, self.clock.now() + wait))
        return done + self.poll()

    def next_deadline(self) -> float | None:
        if not self.pending or self.faults.active("i2c", "stuck"):
            return None
        return self.pending[0].not_before

    def poll(self) -> list[tuple[int, int]]:
        done: list[tuple[int, int]] = []
        now = self.clock.now()
        while self.pending and not self.faults.active("i2c", "stuck") and self.pending[0].not_before <= now:
            done.extend(self._run(self.pending.pop(0).group))
        return done

    def _run(self, group: list[I2cRequest]) -> list[tuple[int, int]]:
        if self.faults.take("i2c", "error") or not all(r.valid for r in group):
            self.failed += 1
            self._trace(group, None)
            if self.events is not None:
                self.events.emit(
                    {
                        "type": "i2c",
                        "segs": [
                            {"addr": r.addr, "dir": "R" if r.read else "W", "data": "", "ok": False} for r in group
                        ],
                        "ok": False,
                        "what": "controller error: transfer refused before the bus",
                    }
                )
            return [(r.chain.head, answer(r, False)) for r in group]
        segs = [Segment(r.addr, r.read, r.data, r.read_len) for r in group]
        results = self.bus.transfer(segs)
        if all(res.ok for res in results):
            self.completed += 1
        else:
            self.failed += 1
        self._trace(group, [res.ok for res in results])
        if self.events is not None:
            segs = [
                (r.addr, r.read, res.data if r.read else r.data, res.ok) for r, res in zip(group, results, strict=True)
            ]
            self.events.emit(
                {
                    "type": "i2c",
                    "segs": [
                        {"addr": a, "dir": "R" if rd else "W", "data": d.hex(), "ok": ok} for a, rd, d, ok in segs
                    ],
                    "ok": all(res.ok for res in results),
                    "what": describe_i2c(segs),
                }
            )
        return [(r.chain.head, answer(r, res.ok, res.data)) for r, res in zip(group, results, strict=True)]

    def _trace(self, group: list[I2cRequest], oks: list[bool] | None) -> None:
        if self.trace is None:
            return
        parts = []
        for i, r in enumerate(group):
            what = f"R{r.read_len}" if r.read else f"W{len(r.data)}"
            state = "err" if oks is None else ("ok" if oks[i] else "nak")
            parts.append(f"{r.addr:#04x} {what} {state}")
        print("i2c  " + " | ".join(parts), file=self.trace, flush=True)
