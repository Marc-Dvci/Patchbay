"""Command line: run the back end, or drive its faults.

python -m patchbay serve --i2c run/i2c.sock --spi run/spi.sock --control run/control.sock
python -m patchbay ctl --control run/control.sock fault nak --bus i2c --target 0x28 --count 3
python -m patchbay ctl --control run/control.sock clear
python -m patchbay ctl --control run/control.sock status
"""

from __future__ import annotations

import argparse
import json
import sys

from .chips.bno055 import Bno055
from .chips.mcp3008 import Mcp3008
from .chips.sht3x import Sht3x
from .clock import FreezableClock
from .control import control
from .events import EventLog
from .faults import KINDS, FaultPlan
from .i2c_bus import I2cBus
from .server import Device, Server
from .spi_bus import SpiBus
from .virtio_i2c import VirtioI2c
from .virtio_spi import VirtioSpi


def build_devices(
    i2c: str | None,
    spi: str | None,
    faults: FaultPlan,
    trace: bool,
    clock: FreezableClock,
    events: EventLog | None = None,
) -> dict[str, tuple[str, Device]]:
    out: dict[str, tuple[str, Device]] = {}
    tracer = sys.stdout if trace else None
    if i2c:
        bus = I2cBus(faults)
        bus.attach(Bno055(clock))
        bus.attach(Sht3x(clock))
        out["i2c"] = (i2c, VirtioI2c(bus, faults, clock, trace=tracer, events=events))
    if spi:
        sbus = SpiBus(cs_count=2, faults=faults)
        sbus.attach(0, Mcp3008(clock))
        out["spi"] = (spi, VirtioSpi(sbus, faults, clock, trace=tracer, events=events))
    return out


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
    s.add_argument("--events", help="append every bus transaction and fault, as JSON lines, to this file")
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
    pn = sub.add_parser("panel", help="the patch panel: a local web page that shows the buses and drives the faults")
    pn.add_argument("--recorded", action="store_true", help="replay the recorded session; needs no QNX and no guest")
    pn.add_argument("--fixture", help="recorded session to replay (default: the committed one)")
    pn.add_argument("--control", help="live: the back end's control socket")
    pn.add_argument("--events", help="live: the back end's events file (serve --events)")
    pn.add_argument("--samples", help="live: directory of the guest samples' output, one <name>.log each")
    pn.add_argument("--port", type=int, default=8093)
    pn.add_argument("--host", default="127.0.0.1", help="address to listen on; other than loopback needs --recorded")
    a = p.parse_args(argv)

    if a.cmd == "panel":
        from .panel import run_panel

        if not a.recorded and not (a.control and a.events):
            p.error("give --recorded, or --control and --events for a live back end")
        return run_panel(a)
    if a.cmd == "serve":
        if not a.i2c and not a.spi:
            p.error("give --i2c, --spi or both")
        faults = FaultPlan()
        clock = FreezableClock()
        events = EventLog(a.events) if a.events else None
        Server(
            build_devices(a.i2c, a.spi, faults, a.trace, clock, events),
            faults,
            a.control,
            clock=clock,
            connect=a.connect,
            events=events,
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
