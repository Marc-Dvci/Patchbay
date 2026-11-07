"""The patch panel: a local web page that shows the buses live and drives the faults.

  python -m patchbay panel --recorded                        replay the committed recorded session (no QNX needed)
  python -m patchbay panel --control C --events E --samples D  live, next to a running back end

Served on 127.0.0.1. --host can name another address, but only for a recorded session, whose fault buttons are
disabled. The page polls /api/events; live, the fault buttons POST to /api/fault, which sends the same control
request as `python -m patchbay ctl fault` and the fault matrix. A recorded session cannot be changed:
its fault buttons are disabled and the faults it shows are the ones injected while it was recorded.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .clock import Clock, MonotonicClock
from .control import control
from .faults import KINDS

STATIC = Path(__file__).with_name("panel_static")
RECORDINGS = Path(__file__).with_name("recordings")
DEFAULT_FIXTURE = RECORDINGS / "session.jsonl.gz"
LOOPBACK = "127.0.0.1"
LOOPBACK_NAMES = ("127.0.0.1", "localhost")
MAX_BATCH = 400
KEEP = 4000

TOPOLOGY: dict[str, Any] = {
    "buses": [
        {"id": "i2c", "name": "I2C bus 1", "node": "/dev/i2c1", "driver": "i2c-virtio"},
        {"id": "spi", "name": "SPI bus 0", "node": "/dev/io-spi/spi0", "driver": "spi-virtio"},
    ],
    "chips": [
        {
            "id": "bno055",
            "bus": "i2c",
            "target": 0x28,
            "name": "BNO055",
            "role": "9-axis IMU",
            "at": "0x28",
            "sample": "imu-bno055",
        },
        {
            "id": "sht3x",
            "bus": "i2c",
            "target": 0x44,
            "name": "SHT3x",
            "role": "temperature, humidity",
            "at": "0x44",
            "sample": "temp-humidity-sht3x",
        },
        {
            "id": "mcp3008",
            "bus": "spi",
            "target": 0,
            "name": "MCP3008",
            "role": "8-channel ADC, joystick",
            "at": "cs0",
            "sample": "mcp-3008-joystick",
        },
    ],
}
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


def load_fixture(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(meta, events), events with t in seconds from the start of the recording."""
    opener: Callable[..., Any] = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    meta = rows[0] if rows and rows[0].get("type") == "meta" else {}
    events = [r for r in rows if r.get("type") != "meta"]
    t0 = events[0]["t"] if events else 0.0
    for e in events:
        e["t"] = round(e["t"] - t0, 4)
    return meta, events


class RecordedSource:
    """Replays a recording on a loop, at the speed it was recorded. Each pass gets new sequence numbers."""

    live = False

    def __init__(self, meta: dict[str, Any], events: list[dict[str, Any]], clock: Clock) -> None:
        if not events:
            raise ValueError("the recording holds no events")
        self.meta = meta
        self.events = events
        self.clock = clock
        self.start = clock.now()
        self.length = events[-1]["t"] + 2.0  # a short pause before the replay starts again

    def head(self) -> int:
        """The sequence number of the latest event due now."""
        elapsed = self.clock.now() - self.start
        lap, into = divmod(elapsed, self.length)
        due = sum(1 for e in self.events if e["t"] <= into)
        return int(lap) * len(self.events) + due

    def since(self, seq: int) -> tuple[list[dict[str, Any]], int]:
        head = self.head()
        first = max(seq, head - MAX_BATCH)
        out = []
        n = len(self.events)
        for s in range(first + 1, head + 1):
            lap, i = divmod(s - 1, n)
            e = dict(self.events[i])
            e["seq"] = s
            e["lap"] = lap
            out.append(e)
        return out, head


class LiveSource:
    """Merges the back end's events file and the samples' output files, as they grow."""

    live = True

    def __init__(self, events_path: Path, samples_dir: Path | None, clock: Clock) -> None:
        self.events_path = events_path
        self.samples_dir = samples_dir
        self.clock = clock
        self.offsets: dict[Path, int] = {}
        self.partial: dict[Path, str] = {}
        self.buffer: list[dict[str, Any]] = []
        self.seq = 0
        self.lock = threading.Lock()
        self.meta = {"label": "live"}

    def _read_new(self, path: Path) -> list[str]:
        try:
            with open(path, "rb") as f:
                f.seek(self.offsets.get(path, 0))
                data = f.read()
                self.offsets[path] = f.tell()
        except FileNotFoundError:
            return []
        text = self.partial.pop(path, "") + data.decode("utf-8", "replace").replace("\r", "\n")
        lines = text.split("\n")
        if lines[-1]:
            self.partial[path] = lines[-1]
        return [line for line in lines[:-1] if line.strip()]

    def _add(self, e: dict[str, Any]) -> None:
        self.seq += 1
        e["seq"] = self.seq
        self.buffer.append(e)
        if len(self.buffer) > KEEP:
            del self.buffer[: len(self.buffer) - KEEP]

    def poll(self) -> None:
        for line in self._read_new(self.events_path):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            e.pop("seq", None)
            self._add(e)
        if self.samples_dir is not None and self.samples_dir.is_dir():
            for log in sorted(self.samples_dir.glob("*.log")):
                for line in self._read_new(log):
                    self._add({"type": "sample", "name": log.stem, "line": line, "t": round(self.clock.now(), 4)})

    def since(self, seq: int) -> tuple[list[dict[str, Any]], int]:
        with self.lock:
            self.poll()
            out = [e for e in self.buffer if e["seq"] > seq][-MAX_BATCH:]
            return out, self.seq


def validate_fault(body: dict[str, Any]) -> dict[str, Any]:
    kind, bus = body.get("kind"), body.get("bus")
    if kind not in KINDS or bus not in ("i2c", "spi"):
        raise ValueError("kind or bus not recognised")
    req: dict[str, Any] = {"cmd": "fault", "kind": kind, "bus": bus}
    target = body.get("target")
    if target is not None:
        if not isinstance(target, int) or not 0 <= target <= 0x7F:
            raise ValueError("target must be an I2C address or a chip select")
        req["target"] = target
    count = body.get("count")
    if count is not None:
        if not isinstance(count, int) or not 1 <= count <= 1000:
            raise ValueError("count must be 1 to 1000")
        req["count"] = count
    if kind == "delay":
        delay = body.get("delay_ms")
        if not isinstance(delay, int) or not 1 <= delay <= 10000:
            raise ValueError("delay_ms must be 1 to 10000")
        req["delay_ms"] = delay
    if kind == "corrupt":
        req["seed"] = int(body.get("seed", 1)) if isinstance(body.get("seed", 1), int) else 1
    return req


class PanelApp:
    """The panel's HTTP surface as one function, so tests call it without a socket."""

    def __init__(
        self,
        source: RecordedSource | LiveSource,
        control_path: str | None = None,
        send: Callable[[str, dict[str, object]], dict[str, object]] = control,
    ) -> None:
        self.source = source
        self.control_path = control_path
        self.send = send

    def handle(self, method: str, target: str, body: bytes = b"") -> tuple[int, str, bytes]:
        url = urlparse(target)
        path = url.path
        if method == "GET" and path == "/api/state":
            mode = "live" if self.source.live else "recorded"
            return self._json(
                200,
                {
                    "mode": mode,
                    "meta": self.source.meta,
                    "topology": TOPOLOGY,
                    "faults_enabled": self.source.live and self.control_path is not None,
                },
            )
        if method == "GET" and path == "/api/events":
            try:
                since = int(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                return self._json(400, {"error": "since must be a number"})
            events, head = self.source.since(max(0, since))
            return self._json(200, {"events": events, "next": head})
        if method == "POST" and path in ("/api/fault", "/api/clear"):
            if not self.source.live or self.control_path is None:
                return self._json(409, {"error": "this is a recorded session; faults can only be injected live"})
            try:
                data = json.loads(body or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                if path == "/api/fault":
                    req: dict[str, Any] = validate_fault(data)
                else:
                    req = {"cmd": "clear"}
                    if data.get("bus") in ("i2c", "spi"):
                        req["bus"] = data["bus"]
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            try:
                return self._json(200, self.send(self.control_path, req))
            except OSError as e:
                return self._json(502, {"error": f"back end not reachable: {e}"})
        if method == "GET":
            name = "index.html" if path == "/" else path.lstrip("/")
            f = (STATIC / name).resolve()
            if f.parent == STATIC.resolve() and f.suffix in STATIC_TYPES and f.is_file():
                return 200, STATIC_TYPES[f.suffix], f.read_bytes()
        return self._json(404, {"error": "not found"})

    @staticmethod
    def _json(status: int, obj: object) -> tuple[int, str, bytes]:
        return status, "application/json", json.dumps(obj, ensure_ascii=False).encode()


def make_server(app: PanelApp, port: int, host: str = LOOPBACK) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, method: str) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, 65536)) if n else b""
            status, ctype, data = app.handle(method, self.path, body)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._reply("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._reply("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer((host, port), Handler)


def run_panel(a: argparse.Namespace) -> int:
    host = getattr(a, "host", LOOPBACK)
    if host not in LOOPBACK_NAMES and not a.recorded:
        print(
            "patch panel: a live panel drives faults, so it listens on loopback only; use --recorded to serve a network",
            file=sys.stderr,
        )
        return 2
    clock = MonotonicClock()
    if a.recorded:
        meta, events = load_fixture(Path(a.fixture) if a.fixture else DEFAULT_FIXTURE)
        source: RecordedSource | LiveSource = RecordedSource(meta, events, clock)
        app = PanelApp(source)
    else:
        source = LiveSource(Path(a.events), Path(a.samples) if a.samples else None, clock)
        app = PanelApp(source, a.control)
    srv = make_server(app, a.port, host)
    mode = "recorded session" if a.recorded else "live"
    print(f"patch panel ({mode}) on http://{host}:{srv.server_address[1]}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
