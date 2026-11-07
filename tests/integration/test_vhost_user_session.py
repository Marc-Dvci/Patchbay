"""The back end's vhost-user state machine, driven by a scripted front end the way QEMU drives it.

Guest memory is a memfd shared through SCM_RIGHTS, kick and call are eventfds, and every message goes over a real
Unix socket. Linux only, like the back end itself.
"""

from __future__ import annotations

import io
import json
import mmap
import os
import socket
import struct
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from patchbay import vhost_user as vu
from patchbay.chips.bno055 import Bno055
from patchbay.clock import FakeClock
from patchbay.faults import Fault, FaultPlan
from patchbay.i2c_bus import I2cBus
from patchbay.server import VIRTIO_RING_F_INDIRECT_DESC, Connection, Server
from patchbay.spi_bus import SpiBus
from patchbay.virtio_i2c import FLAGS_FAIL_NEXT, FLAGS_M_RD, MSG_OK, VirtioI2c
from patchbay.virtio_spi import VirtioSpi
from tests.ringkit import Arena, DriverRing

pytestmark = pytest.mark.integration

GPA = 0x100000
UVA = 0x7F12_0000_0000
SIZE = 1 << 20


class FrontEnd:
    """Plays QEMU's part: sends vhost-user messages, owns guest memory and the event fds."""

    def __init__(self, conn: Connection, sock: socket.socket) -> None:
        self.conn = conn
        self.sock = sock
        self.memfd = os.memfd_create("guest-ram")
        os.ftruncate(self.memfd, SIZE)
        self.mm = mmap.mmap(self.memfd, SIZE)
        self.arena = Arena(SIZE, GPA, memoryview(self.mm))
        self.kick = os.eventfd(0, os.EFD_NONBLOCK)
        self.call = os.eventfd(0, os.EFD_NONBLOCK)

    def send(self, req: int, payload: bytes = b"", fds: list[int] | None = None, need_reply: bool = False) -> bytes:
        flags = vu.VERSION | (vu.FLAG_NEED_REPLY if need_reply else 0)
        data = struct.pack("<III", req, flags, len(payload)) + payload
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack(f"{len(fds)}i", *fds))] if fds else []
        self.sock.sendmsg([data], anc)
        got = self.conn.recv()
        assert got is not None
        self.conn.handle(*got)
        self.sock.setblocking(False)
        try:
            return self.sock.recv(65536)
        except BlockingIOError:
            return b""
        finally:
            self.sock.setblocking(True)

    def u64(self, req: int, value: int) -> bytes:
        return self.send(req, struct.pack("<Q", value))

    def setup(self, ring: DriverRing, protocol: bool = True) -> None:
        self.send(vu.Req.SET_OWNER)
        feats = struct.unpack_from("<Q", self.send(vu.Req.GET_FEATURES), 12)[0]
        want = vu.VIRTIO_F_VERSION_1 | (vu.F_PROTOCOL_FEATURES if protocol else 0)
        assert feats & want == want
        self.u64(vu.Req.SET_FEATURES, want)
        if protocol:
            proto = struct.unpack_from("<Q", self.send(vu.Req.GET_PROTOCOL_FEATURES), 12)[0]
            self.u64(vu.Req.SET_PROTOCOL_FEATURES, proto)
        table = struct.pack("<II", 1, 0) + struct.pack("<QQQQ", GPA, SIZE, UVA, 0)
        self.send(vu.Req.SET_MEM_TABLE, table, [self.memfd])
        self.send(vu.Req.SET_VRING_NUM, struct.pack("<II", 0, ring.size))
        self.send(vu.Req.SET_VRING_BASE, struct.pack("<II", 0, 0))
        uva = lambda gpa: UVA + gpa - GPA  # noqa: E731
        addr = struct.pack("<IIQQQQ", 0, 0, uva(ring.desc), uva(ring.used), uva(ring.avail), 0)
        self.send(vu.Req.SET_VRING_ADDR, addr)
        self.send(vu.Req.SET_VRING_KICK, struct.pack("<Q", 0), [self.kick])
        self.send(vu.Req.SET_VRING_CALL, struct.pack("<Q", 0), [self.call])
        if protocol:
            self.send(vu.Req.SET_VRING_ENABLE, struct.pack("<II", 0, 1))

    def notify(self) -> None:
        os.write(self.kick, (1).to_bytes(8, "little"))
        self.conn.service(self.conn.queues[0], kicked=True)

    def called(self) -> bool:
        try:
            return int.from_bytes(os.read(self.call, 8), "little") > 0
        except BlockingIOError:
            return False


def i2c_device(plan: FaultPlan | None = None) -> VirtioI2c:
    clock = FakeClock(5.0)
    plan = plan or FaultPlan()
    bus = I2cBus(plan)
    bus.attach(Bno055(clock))
    return VirtioI2c(bus, plan, clock)


@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    yield a, b
    a.close()
    b.close()


def chip_id_request(ring: DriverRing) -> int:
    ring.add([struct.pack("<HHI", 0x28 << 1, 0, FLAGS_FAIL_NEXT), b"\x00"], [1])
    _, ins = ring.add([struct.pack("<HHI", 0x28 << 1, 0, FLAGS_M_RD)], [2])
    return ins[0]


def test_a_full_session_reads_the_chip_id_and_signals_the_call_fd(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    fe = FrontEnd(Connection("i2c", b, i2c_device(), io.StringIO()), a)
    ring = DriverRing(fe.arena, size=4)
    fe.setup(ring)
    assert fe.conn.queues[0].ring is not None
    rx = chip_id_request(ring)
    fe.notify()
    assert fe.arena.read(rx, 2) == bytes([0xA0, MSG_OK])
    assert fe.called()
    assert len(ring.reap()) == 2


def test_without_protocol_features_the_queue_starts_on_the_kick_fd(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    fe = FrontEnd(Connection("i2c", b, i2c_device(), io.StringIO()), a)
    ring = DriverRing(fe.arena, size=4)
    fe.setup(ring, protocol=False)
    assert fe.conn.queues[0].ring is not None


def test_with_protocol_features_the_queue_waits_for_set_vring_enable(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    fe = FrontEnd(Connection("i2c", b, i2c_device(), io.StringIO()), a)
    ring = DriverRing(fe.arena, size=4)
    fe.setup(ring)
    fe.send(vu.Req.GET_VRING_BASE, struct.pack("<II", 0, 0))
    fe.send(vu.Req.SET_VRING_KICK, struct.pack("<Q", 0), [os.eventfd(0)])
    assert fe.conn.queues[0].ring is not None  # still enabled from before: the kick restarts it
    fe.send(vu.Req.SET_VRING_ENABLE, struct.pack("<II", 0, 0))
    fe.send(vu.Req.GET_VRING_BASE, struct.pack("<II", 0, 0))
    fe.send(vu.Req.SET_VRING_KICK, struct.pack("<Q", 0), [os.eventfd(0)])
    assert fe.conn.queues[0].ring is None


def test_get_vring_base_stops_the_queue_and_drops_a_stuck_request(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    plan = FaultPlan()
    dev = i2c_device(plan)
    fe = FrontEnd(Connection("i2c", b, dev, io.StringIO()), a)
    ring = DriverRing(fe.arena, size=4)
    fe.setup(ring)
    plan.add(Fault("stuck", "i2c"))
    chip_id_request(ring)
    fe.notify()
    assert ring.reap() == [] and dev.pending
    reply = fe.send(vu.Req.GET_VRING_BASE, struct.pack("<II", 0, 0))
    idx, base = struct.unpack_from("<II", reply, 12)
    assert (idx, base) == (0, 2)  # both requests were taken off the available ring
    assert fe.conn.queues[0].ring is None and not dev.pending


def test_reply_ack_is_sent_when_asked_for_and_negotiated(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    fe = FrontEnd(Connection("i2c", b, i2c_device(), io.StringIO()), a)
    ring = DriverRing(fe.arena, size=4)
    fe.setup(ring)
    reply = fe.send(vu.Req.SET_VRING_NUM, struct.pack("<II", 0, 4), need_reply=True)
    req, flags, size = struct.unpack_from("<III", reply)
    assert req == vu.Req.SET_VRING_NUM and flags & vu.FLAG_REPLY and size == 8


def test_the_spi_device_offers_config_and_answers_get_config(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    dev = VirtioSpi(SpiBus(), FaultPlan(), FakeClock())
    fe = FrontEnd(Connection("spi", b, dev, io.StringIO()), a)
    proto = struct.unpack_from("<Q", fe.send(vu.Req.GET_PROTOCOL_FEATURES), 12)[0]
    assert proto & vu.Proto.CONFIG
    reply = fe.send(vu.Req.GET_CONFIG, struct.pack("<III", 0, 32, 0) + bytes(32))
    assert reply[12 + 12 :] == dev.config


def test_the_i2c_device_does_not_offer_config(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    fe = FrontEnd(Connection("i2c", b, i2c_device(), io.StringIO()), a)
    proto = struct.unpack_from("<Q", fe.send(vu.Req.GET_PROTOCOL_FEATURES), 12)[0]
    assert not proto & vu.Proto.CONFIG
    feats = struct.unpack_from("<Q", fe.send(vu.Req.GET_FEATURES), 12)[0]
    assert feats & VIRTIO_RING_F_INDIRECT_DESC and feats & 1  # ZERO_LENGTH_REQUEST


def test_a_memory_table_with_a_missing_descriptor_is_refused(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    fe = FrontEnd(Connection("i2c", b, i2c_device(), io.StringIO()), a)
    table = struct.pack("<II", 1, 0) + struct.pack("<QQQQ", GPA, SIZE, UVA, 0)
    with pytest.raises(vu.ProtocolError, match="descriptors"):
        fe.send(vu.Req.SET_MEM_TABLE, table, [])


def test_a_broken_chain_stops_the_queue_and_is_logged(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    log = io.StringIO()
    fe = FrontEnd(Connection("i2c", b, i2c_device(), log), a)
    ring = DriverRing(fe.arena, size=4)
    fe.setup(ring)
    ring.write_desc(ring.desc, 0, 0xDEAD_0000, 8, 0, 0)  # outside shared memory
    ring.publish_head(0)
    fe.notify()
    assert fe.conn.queues[0].ring is None
    assert "driver error" in log.getvalue()


def test_the_server_takes_fault_commands_on_its_control_socket() -> None:
    with tempfile.TemporaryDirectory() as d:
        ctl = str(Path(d) / "control.sock")
        plan = FaultPlan()
        server = Server({"i2c": (str(Path(d) / "i2c.sock"), i2c_device(plan))}, plan, ctl, log=io.StringIO())
        t = threading.Thread(target=server.run, daemon=True)
        t.start()

        def ask(req: dict[str, object]) -> dict[str, object]:
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(ctl)
                s.sendall((json.dumps(req) + "\n").encode())
                return json.loads(s.makefile().readline())  # type: ignore[no-any-return]

        assert ask({"cmd": "fault", "kind": "nak", "bus": "i2c", "target": 0x28})["ok"]
        assert plan.faults and plan.faults[0].kind == "nak"
        assert ask({"cmd": "fault", "kind": "melt", "bus": "i2c"})["ok"] is False
        status = ask({"cmd": "status"})
        assert status["devices"] == {"i2c": {"connected": False, "completed": 0, "failed": 0}}
        assert ask({"cmd": "clear"})["cleared"] == 1
        assert ask({"cmd": "freeze"})["ok"] is False  # this server was given no freezable clock
        assert ask({"cmd": "dance"})["ok"] is False
        assert ask({"cmd": "stop"})["ok"]
        t.join(timeout=5)
        assert not t.is_alive()
