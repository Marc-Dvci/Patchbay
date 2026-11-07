"""A panel fault button reaches the running back end through its control socket, the path the fault matrix uses,
and the back end's fault event comes back to the panel."""

from __future__ import annotations

import io
import json
import tempfile
import threading
from pathlib import Path

import pytest

from patchbay.chips.bno055 import Bno055
from patchbay.clock import FakeClock
from patchbay.control import control
from patchbay.events import EventLog
from patchbay.faults import FaultPlan
from patchbay.i2c_bus import I2cBus
from patchbay.panel import LiveSource, PanelApp
from patchbay.server import Server
from patchbay.virtio_i2c import VirtioI2c

pytestmark = pytest.mark.integration


def test_the_pull_cord_button_unplugs_the_chip_in_the_back_end_and_shows_up_as_an_event() -> None:
    with tempfile.TemporaryDirectory() as d:
        ctl = str(Path(d) / "c.sock")
        ev = Path(d) / "events.jsonl"
        clock = FakeClock(1.0)
        plan = FaultPlan()
        bus = I2cBus(plan)
        bus.attach(Bno055(clock))
        events = EventLog(ev, clock)
        dev = VirtioI2c(bus, plan, clock, events=events)
        server = Server({"i2c": (str(Path(d) / "i2c.sock"), dev)}, plan, ctl, log=io.StringIO(), events=events)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        app = PanelApp(LiveSource(ev, None, clock), ctl)
        status, _, body = app.handle("POST", "/api/fault", b'{"kind":"unplug","bus":"i2c","target":40}')
        assert status == 200 and json.loads(body)["ok"]
        assert [(f.kind, f.target) for f in plan.faults] == [("unplug", 0x28)]
        _, _, body = app.handle("GET", "/api/events?since=0")
        got = json.loads(body)["events"]
        assert got[0]["type"] == "fault" and got[0]["fault"]["kind"] == "unplug"
        control(ctl, {"cmd": "stop"})
        t.join(timeout=5)
