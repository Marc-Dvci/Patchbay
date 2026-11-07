"""The back-end process: one vhost-user socket per device, a control socket, one event loop, no threads.

A device object (VirtioI2c, VirtioSpi) turns available chains into completions; this module owns the sockets, the
shared memory, the kick and call event descriptors, and the vhost-user state machine around the one request queue.
"""

from __future__ import annotations

import array
import json
import mmap
import os
import selectors
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol, TextIO

from . import vhost_user as vu
from .clock import FreezableClock
from .events import EventLog
from .faults import Fault, FaultPlan
from .memory import GuestMemory, MemoryFault, Region
from .vring import DeviceRing, RingError

VIRTIO_RING_F_INDIRECT_DESC = 1 << 28


class Device(Protocol):
    device_id: int
    device_features: int
    num_queues: int

    @property
    def config(self) -> bytes: ...

    def reset(self) -> None: ...

    def kick(self, ring: DeviceRing) -> list[tuple[int, int]]: ...

    def poll(self) -> list[tuple[int, int]]: ...

    def next_deadline(self) -> float | None: ...


@dataclass
class Queue:
    num: int = 0
    base: int = 0
    addr: tuple[int, int, int] | None = None  # desc, used, avail, front-end addresses
    kick: int = -1
    call: int = -1
    enabled: bool = False
    ring: DeviceRing | None = None


@dataclass
class Connection:
    """One vhost-user front end (QEMU) and the device it drives."""

    name: str
    sock: socket.socket
    device: Device
    log: TextIO
    features: int = 0
    protocol: int = 0
    mem: GuestMemory = field(default_factory=GuestMemory)
    maps: list[mmap.mmap] = field(default_factory=list)
    queues: list[Queue] = field(default_factory=list)
    broken: bool = False
    debug: bool = field(default_factory=lambda: bool(os.environ.get("PATCHBAY_DEBUG")))

    def __post_init__(self) -> None:
        self.queues = [Queue() for _ in range(self.device.num_queues)]

    # --- offered features ------------------------------------------------------------------------------------------
    def offered_features(self) -> int:
        return (
            vu.VIRTIO_F_VERSION_1 | VIRTIO_RING_F_INDIRECT_DESC | vu.F_PROTOCOL_FEATURES | self.device.device_features
        )

    def offered_protocol(self) -> int:
        p = vu.Proto.REPLY_ACK
        if self.device.config:
            p |= vu.Proto.CONFIG
        return int(p)

    # --- messages --------------------------------------------------------------------------------------------------
    def recv(self) -> tuple[vu.Message, list[int]] | None:
        fds = array.array("i")
        try:
            raw, anc, _flags, _addr = self.sock.recvmsg(vu.HEADER.size, socket.CMSG_LEN(vu.MAX_FDS * fds.itemsize))
        except ConnectionError:
            return None
        if not raw:
            return None
        for level, typ, data in anc:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                fds.frombytes(data[: len(data) - (len(data) % fds.itemsize)])
        req, flags, size = vu.decode_header(raw)
        payload = b""
        while len(payload) < size:
            chunk = self.sock.recv(size - len(payload))
            if not chunk:
                return None
            payload += chunk
        return vu.Message(req, flags, payload), list(fds)

    def reply(self, req: int, payload: bytes) -> None:
        self.sock.sendall(vu.encode_reply(req, payload))

    def handle(self, msg: vu.Message, fds: list[int]) -> None:
        """Apply one message. Any fd not kept is closed."""
        if self.debug:
            try:
                name = vu.Req(msg.req).name
            except ValueError:
                name = str(msg.req)
            self.say(f"<- {name} flags={msg.flags:#x} size={len(msg.payload)} fds={len(fds)}")
        kept: set[int] = set()
        replied = False
        try:
            r = msg.req
            if r == vu.Req.GET_FEATURES:
                self.reply(r, self.offered_features().to_bytes(8, "little"))
                replied = True
            elif r == vu.Req.SET_FEATURES:
                self.features = vu.u64(msg.payload)
            elif r == vu.Req.GET_PROTOCOL_FEATURES:
                self.reply(r, self.offered_protocol().to_bytes(8, "little"))
                replied = True
            elif r == vu.Req.SET_PROTOCOL_FEATURES:
                self.protocol = vu.u64(msg.payload) & self.offered_protocol()
            elif r == vu.Req.SET_OWNER:
                pass
            elif r == vu.Req.RESET_OWNER:
                self.stop_all()
            elif r == vu.Req.GET_QUEUE_NUM:
                self.reply(r, len(self.queues).to_bytes(8, "little"))
                replied = True
            elif r == vu.Req.SET_MEM_TABLE:
                self.set_mem_table(vu.decode_mem_table(msg.payload), fds)
            elif r == vu.Req.SET_VRING_NUM:
                idx, num = vu.decode_vring_state(msg.payload)
                self.queue(idx).num = num
            elif r == vu.Req.SET_VRING_BASE:
                idx, base = vu.decode_vring_state(msg.payload)
                self.queue(idx).base = base
            elif r == vu.Req.SET_VRING_ADDR:
                idx, _flags, desc, used, avail = vu.decode_vring_addr(msg.payload)
                self.queue(idx).addr = (desc, used, avail)
            elif r == vu.Req.SET_VRING_KICK:
                q, fd = self.vring_fd(msg, fds)
                if q.kick >= 0:
                    os.close(q.kick)
                q.kick = fd
                kept.add(fd)
                if not self.features & vu.F_PROTOCOL_FEATURES:
                    q.enabled = True
                self.start(q)
            elif r == vu.Req.SET_VRING_CALL:
                q, fd = self.vring_fd(msg, fds)
                if q.call >= 0:
                    os.close(q.call)
                q.call = fd
                kept.add(fd)
            elif r == vu.Req.SET_VRING_ERR:
                pass
            elif r == vu.Req.SET_VRING_ENABLE:
                idx, on = vu.decode_vring_state(msg.payload)
                q = self.queue(idx)
                q.enabled = bool(on)
                self.start(q)
            elif r == vu.Req.GET_VRING_BASE:
                idx, _ = vu.decode_vring_state(msg.payload)
                q = self.queue(idx)
                base = q.ring.last_avail if q.ring is not None else q.base
                self.stop(q)
                self.reply(r, vu.VRING_STATE.pack(idx, base))
                replied = True
            elif r == vu.Req.GET_CONFIG:
                off, size, flags, _ = vu.decode_config_request(msg.payload)
                self.reply(r, vu.encode_config_reply(off, size, flags, self.device.config))
                replied = True
            elif r == vu.Req.SET_CONFIG:
                pass  # every config field of both devices is read-only for the driver
            else:
                self.say(f"ignoring vhost-user request {r}")
            if msg.need_reply and not replied and self.protocol & vu.Proto.REPLY_ACK:
                self.reply(r, (0).to_bytes(8, "little"))
        finally:
            for fd in fds:
                if fd not in kept:
                    os.close(fd)

    def queue(self, idx: int) -> Queue:
        if not 0 <= idx < len(self.queues):
            raise vu.ProtocolError(f"queue {idx} does not exist")
        return self.queues[idx]

    def vring_fd(self, msg: vu.Message, fds: list[int]) -> tuple[Queue, int]:
        v = vu.u64(msg.payload)
        q = self.queue(v & vu.VRING_IDX_MASK)
        if v & vu.VRING_NOFD_MASK or len(fds) != 1:
            raise vu.ProtocolError("polling mode (no event fd) is not supported")
        return q, fds[0]

    def set_mem_table(self, regions: list[vu.MemRegion], fds: list[int]) -> None:
        if len(fds) != len(regions):
            raise vu.ProtocolError(f"{len(regions)} regions but {len(fds)} descriptors")
        maps, out = [], []
        for reg, fd in zip(regions, fds, strict=True):
            mm = mmap.mmap(fd, reg.mmap_offset + reg.size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            maps.append(mm)
            out.append(Region(reg.gpa, reg.size, reg.uva, memoryview(mm)[reg.mmap_offset : reg.mmap_offset + reg.size]))
        self.maps = maps
        self.mem = GuestMemory(out)
        for q in self.queues:  # rings follow the new table
            if q.ring is not None:
                q.ring.mem = self.mem

    # --- queue life cycle ------------------------------------------------------------------------------------------
    def start(self, q: Queue) -> None:
        if q.ring is not None or not q.enabled or q.kick < 0 or q.addr is None or q.num == 0:
            return
        desc, used, avail = q.addr
        try:
            q.ring = DeviceRing(
                self.mem,
                q.num,
                self.mem.uva_to_gpa(desc),
                self.mem.uva_to_gpa(avail),
                self.mem.uva_to_gpa(used),
                last_avail=q.base,
                indirect=bool(self.features & VIRTIO_RING_F_INDIRECT_DESC),
            )
        except (MemoryFault, RingError) as e:
            self.say(f"queue not started: {e}")
            return
        self.say(f"queue started, {q.num} entries")
        self.service(q)

    def stop(self, q: Queue) -> None:
        if q.ring is not None:
            self.say("queue stopped")
        q.ring = None
        if q.kick >= 0:
            os.close(q.kick)
            q.kick = -1
        self.device.reset()

    def stop_all(self) -> None:
        for q in self.queues:
            self.stop(q)
            if q.call >= 0:
                os.close(q.call)
                q.call = -1

    def service(self, q: Queue, kicked: bool = False) -> None:
        if q.ring is None:
            return
        try:
            done = self.device.kick(q.ring) if kicked or q.ring.pending() else self.device.poll()
        except RingError as e:
            self.say(f"driver error, queue stopped: {e}")
            q.ring = None
            return
        self.complete(q, done)

    def complete(self, q: Queue, done: list[tuple[int, int]]) -> None:
        if not done or q.ring is None:
            return
        for head, written in done:
            q.ring.push(head, written)
        if q.call >= 0 and q.ring.wants_interrupt():
            os.write(q.call, (1).to_bytes(8, "little"))

    def say(self, text: str) -> None:
        print(f"[{self.name}] {text}", file=self.log, flush=True)

    def close(self) -> None:
        self.stop_all()
        self.maps = []
        self.sock.close()


class Server:
    """Listens on one Unix socket per device, accepts one front end per socket at a time, runs until stopped."""

    def __init__(
        self,
        devices: dict[str, tuple[str, Device]],
        faults: FaultPlan,
        control_path: str | None,
        log: TextIO = sys.stdout,
        clock: FreezableClock | None = None,
        connect: bool = False,
        events: EventLog | None = None,
    ) -> None:
        """connect=False: listen on each device's socket path, QEMU connects (QEMU's default, client mode).
        connect=True: QEMU listens (server=on) and this back end connects, and connects again after losing the
        connection, so a back end that restarts is picked up by the running guest."""
        self.devices = devices
        self.faults = faults
        self.log = log
        self.clock = clock
        self.connect = connect
        self.events = events
        self.retry_at: dict[str, float] = {}
        self.sel = selectors.DefaultSelector()
        self.listeners: dict[str, socket.socket] = {}
        self.conns: dict[str, Connection] = {}
        self.running = True
        for name, (path, _dev) in devices.items():
            if connect:
                self.retry_at[name] = 0.0
                continue
            self.listeners[name] = self._listen(path)
            self.sel.register(self.listeners[name], selectors.EVENT_READ, ("listen", name))
        self.control: socket.socket | None = None
        if control_path:
            self.control = self._listen(control_path)
            self.sel.register(self.control, selectors.EVENT_READ, ("control-listen", None))

    @staticmethod
    def _listen(path: str) -> socket.socket:
        """A new listening socket. An existing path is refused rather than removed: give each run its own path."""
        if os.path.exists(path):
            raise FileExistsError(f"{path} exists; choose a new socket path")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        s.listen(1)
        return s

    def say(self, text: str) -> None:
        print(f"[patchbay] {text}", file=self.log, flush=True)

    def run(self) -> None:
        self.say("ready: " + ", ".join(f"{n} on {p}" for n, (p, _) in self.devices.items()))
        while self.running:
            self._retry_connects()
            timeout = self._timeout()
            for key, _ev in self.sel.select(timeout):
                kind, name = key.data
                if kind == "listen":
                    self._accept(name)
                elif kind == "msg":
                    self._message(name)
                elif kind == "kick":
                    self._kick(name, key.fd)
                elif kind == "control-listen":
                    assert self.control is not None
                    c, _ = self.control.accept()
                    self.sel.register(c, selectors.EVENT_READ, ("control", None))
                elif kind == "control":
                    self._control(key.fileobj)  # type: ignore[arg-type]
            for conn in list(self.conns.values()):
                for q in conn.queues:
                    conn.service(q)

    def _timeout(self) -> float | None:
        deadlines = [d for c in self.conns.values() if (d := c.device.next_deadline()) is not None]
        deadlines += list(self.retry_at.values())
        if self.faults.faults and any(f.kind == "stuck" for f in self.faults.faults):
            return 0.2  # a stuck fault may be cleared from the control socket; look again soon
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - time.monotonic())

    def _retry_connects(self) -> None:
        now = time.monotonic()
        for name, at in list(self.retry_at.items()):
            if at > now:
                continue
            path, dev = self.devices[name]
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(path)
            except OSError:
                sock.close()
                self.retry_at[name] = now + 0.2
                continue
            del self.retry_at[name]
            self._attach(name, sock)

    def _accept(self, name: str) -> None:
        sock, _ = self.listeners[name].accept()
        if name in self.conns:
            self._drop(name)
        self._attach(name, sock)

    def _attach(self, name: str, sock: socket.socket) -> None:
        _path, dev = self.devices[name]
        self.conns[name] = Connection(name, sock, dev, self.log)
        self.sel.register(sock, selectors.EVENT_READ, ("msg", name))
        self.say(f"{name}: front end connected")

    def _drop(self, name: str) -> None:
        conn = self.conns.pop(name)
        for fileobj in [q.kick for q in conn.queues if q.kick >= 0] + [conn.sock]:
            try:
                self.sel.unregister(fileobj)
            except (KeyError, ValueError):
                pass
        conn.close()
        self.say(f"{name}: front end gone")
        if self.connect:
            self.retry_at[name] = time.monotonic() + 0.5

    def _message(self, name: str) -> None:
        conn = self.conns[name]
        before = [q.kick for q in conn.queues]
        try:
            got = conn.recv()
            if got is None:
                self._drop(name)
                return
            msg, fds = got
            # unregister kick fds that the message may close before the message runs
            for fd in before:
                if fd >= 0:
                    self.sel.unregister(fd)
            try:
                conn.handle(msg, fds)
            finally:
                for q in conn.queues:
                    if q.kick >= 0:
                        self.sel.register(q.kick, selectors.EVENT_READ, ("kick", conn.name))
        except (vu.ProtocolError, OSError) as e:
            self.say(f"{name}: {e}; closing")
            self._drop(name)

    def _kick(self, name: str, fd: int) -> None:
        conn = self.conns.get(name)
        if conn is None:
            return
        try:
            os.read(fd, 8)
        except BlockingIOError:
            return
        for q in conn.queues:
            if q.kick == fd:
                conn.service(q, kicked=True)

    def _control(self, sock: socket.socket) -> None:
        try:
            data = sock.recv(65536)
        except ConnectionError:
            data = b""
        if not data:
            self.sel.unregister(sock)
            sock.close()
            return
        for line in data.decode("utf-8", "replace").splitlines():
            if line.strip():
                sock.sendall((json.dumps(self.command(line)) + "\n").encode())

    def command(self, line: str) -> dict[str, object]:
        """One control request, a JSON object, and its answer."""
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "fault":
                f = Fault(
                    kind=req["kind"],
                    bus=req["bus"],
                    target=req.get("target"),
                    count=req.get("count"),
                    delay_ms=int(req.get("delay_ms", 0)),
                    seed=int(req.get("seed", 0)),
                )
                self.faults.add(f)
                self.say(f"fault on: {f.describe()}")
                if self.events is not None:
                    self.events.emit({"type": "fault", "action": "add", "fault": f.describe()})
                return {"ok": True, "faults": self.faults.describe()}
            if cmd == "clear":
                n = self.faults.clear(req.get("bus"))
                self.say(f"faults cleared: {n}")
                if self.events is not None:
                    self.events.emit({"type": "fault", "action": "clear", "bus": req.get("bus"), "cleared": n})
                return {"ok": True, "cleared": n}
            if cmd == "status":
                out: dict[str, object] = {"ok": True, "faults": self.faults.describe(), "devices": {}}
                for name, (_p, dev) in self.devices.items():
                    out["devices"][name] = {  # type: ignore[index]
                        "connected": name in self.conns,
                        "completed": getattr(dev, "completed", 0),
                        "failed": getattr(dev, "failed", 0),
                    }
                return out
            if cmd in ("freeze", "thaw"):
                if self.clock is None:
                    return {"ok": False, "error": "this back end's clock cannot be frozen"}
                if cmd == "freeze":
                    self.clock.freeze()
                else:
                    self.clock.thaw()
                self.say(f"simulated time {'stopped' if cmd == 'freeze' else 'running'}")
                return {"ok": True}
            if cmd == "stop":
                self.running = False
                return {"ok": True}
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        except (ValueError, KeyError, TypeError) as e:
            return {"ok": False, "error": str(e)}
