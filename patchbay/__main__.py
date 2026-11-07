"""Command line: run the back end, or drive its faults.

python -m patchbay serve --i2c run/i2c.sock --spi run/spi.sock --control run/control.sock
python -m patchbay ctl --control run/control.sock fault nak --bus i2c --target 0x28 --count 3
python -m patchbay ctl --control run/control.sock clear
python -m patchbay ctl --control run/control.sock status
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

from .chips.bno055 import Bno055
from .chips.mcp3008 import Mcp3008
from .chips.sht3x import Sht3x
from .clock import FreezableClock
from .faults import KINDS, FaultPlan
from .i2c_bus import I2cBus
from .server import Device, Server
from .spi_bus import SpiBus
from .virtio_i2c import VirtioI2c
from .virtio_spi import VirtioSpi


def build_devices(
    i2c: str | None, spi: str | None, faults: FaultPlan, trace: bool, clock: FreezableClock
) -> dict[str, tuple[str, Device]]:
    out: dict[str, tuple[str, Device]] = {}
    tracer = sys.stdout if trace else None
    if i2c:
        bus = I2cBus(faults)
        bus.attach(Bno055(clock))
        bus.attach(Sht3x(clock))
        out["i2c"] = (i2c, VirtioI2c(bus, faults, clock, trace=tracer))
    if spi:
        sbus = SpiBus(cs_count=2, faults=faults)
        sbus.attach(0, Mcp3008(clock))
        out["spi"] = (spi, VirtioSpi(sbus, faults, clock, trace=tracer))
    return out


def control(path: str, request: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(path)
        s.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="patchbay")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the vhost-user back end")
    s.add_argument("--i2c", help="vhost-user socket path for the I2C controller")
    s.add_argument("--spi", help="vhost-user socket path for the SPI controller")
    s.add_argument("--control", help="control socket path")
    s.add_argument("--trace", action="store_true", help="print every bus transaction")
    s.add_argument(
        "--connect",
        action="store_true",
        help="connect to sockets QEMU listens on (server=on), and reconnect after a restart",
    )
    c = sub.add_parser("ctl", help="talk to a running back end")
    c.add_argument("--control", required=True)
    csub = c.add_subparsers(dest="action", required=True)
    f = csub.add_parser("fault")
    f.add_argument("kind", choices=KINDS)
    f.add_argument("--bus", choices=("i2c", "spi"), required=True)
    f.add_argument("--target", type=lambda v: int(v, 0))
    f.add_argument("--count", type=int)
    f.add_argument("--delay-ms", type=int, default=0)
    f.add_argument("--seed", type=int, default=0)
    cl = csub.add_parser("clear")
    cl.add_argument("--bus", choices=("i2c", "spi"))
    csub.add_parser("status")
    csub.add_parser("freeze", help="stop simulated time: sensor values hold still")
    csub.add_parser("thaw", help="start simulated time again")
    csub.add_parser("stop")
    a = p.parse_args(argv)

    if a.cmd == "serve":
        if not a.i2c and not a.spi:
            p.error("give --i2c, --spi or both")
        faults = FaultPlan()
        clock = FreezableClock()
        Server(
            build_devices(a.i2c, a.spi, faults, a.trace, clock), faults, a.control, clock=clock, connect=a.connect
        ).run()
        return 0
    req: dict[str, object] = {"cmd": a.action}
    if a.action == "fault":
        req.update(kind=a.kind, bus=a.bus, target=a.target, count=a.count, delay_ms=a.delay_ms, seed=a.seed)
    elif a.action == "clear" and a.bus:
        req["bus"] = a.bus
    reply = control(a.control, req)
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
