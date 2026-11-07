"""Records a session for the panel's --recorded mode, from a booted guest.

    python3 scripts/record_session.py [--out patchbay/recordings/session.jsonl.gz]

Starts QNX's three samples in the guest, unmodified, streams their output back over ssh, and follows the back end's
events file (scripts/run_guest.sh start writes it). Meanwhile it plays a fixed script of faults through the control
socket, the same path the fault matrix and the panel's buttons use. Everything goes into one gzip'd JSON-lines file,
with the host's monotonic clock on every event: bus transactions and faults stamped by the back end, sample lines
stamped as they arrive here. The first line names the run.
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "scripts" / "run_guest.sh"
WORK = Path.home() / "Patchbay-work" / "run"
SAMPLES = ["imu-bno055", "temp-humidity-sht3x", "mcp-3008-joystick"]

# (seconds after start, what to say, control arguments or None for "clear")
STORY: list[tuple[float, str, list[str] | None]] = [
    (10.0, "Pull the BNO055's cord", ["fault", "unplug", "--bus", "i2c", "--target", "0x28"]),
    (16.0, "Cord back in", None),
    (
        20.0,
        "SHT3x does not acknowledge, 3 transfers",
        ["fault", "nak", "--bus", "i2c", "--target", "0x44", "--count", "3"],
    ),
    (
        24.0,
        "Corrupt 4 SHT3x readings",
        ["fault", "corrupt", "--bus", "i2c", "--target", "0x44", "--count", "4", "--seed", "3"],
    ),
    (
        28.0,
        "Corrupt 6 MCP3008 readings",
        ["fault", "corrupt", "--bus", "spi", "--target", "0", "--count", "6", "--seed", "5"],
    ),
    (32.0, "Stuck I2C bus", ["fault", "stuck", "--bus", "i2c"]),
    (36.0, "Bus released", None),
    (
        39.0,
        "SPI controller answers 1500 ms late, twice",
        ["fault", "delay", "--bus", "spi", "--delay-ms", "1500", "--count", "2"],
    ),
    (46.0, "All faults cleared", None),
]
LENGTH = 50.0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(ROOT / "patchbay" / "recordings" / "session.jsonl.gz"))
    a = p.parse_args()
    events_file = Path((WORK / "events.path").read_text().strip())
    offset = events_file.stat().st_size if events_file.exists() else 0
    collected: list[dict[str, object]] = []
    lock = threading.Lock()
    t0 = time.monotonic()

    def stream(name: str) -> None:
        d = f"/tmp/rec-{name}-{int(time.time())}"
        cmd = f"mkdir -p {d} && cd {d} && exec {name}"
        proc = subprocess.Popen([str(RUN), "ssh", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        buf = b""
        while True:
            ch = proc.stdout.read1(4096)
            if not ch:
                break
            buf += ch.replace(b"\r", b"\n")
            *lines, buf = buf.split(b"\n")
            now = round(time.monotonic(), 4)
            with lock:
                for raw in lines:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line:
                        collected.append({"type": "sample", "name": name, "line": line, "t": now})

    threads = [threading.Thread(target=stream, args=(n,), daemon=True) for n in SAMPLES]
    for t in threads:
        t.start()
    for at, say, args in STORY:
        time.sleep(max(0.0, t0 + at - time.monotonic()))
        with lock:
            collected.append({"type": "note", "line": say, "t": round(time.monotonic(), 4)})
        subprocess.run([str(RUN), "ctl", *(args or ["clear"])], check=False, capture_output=True)
        print(f"{at:5.1f} s  {say}", flush=True)
    time.sleep(max(0.0, t0 + LENGTH - time.monotonic()))
    subprocess.run([str(RUN), "ssh", "slay -f " + " ".join(SAMPLES)], check=False, capture_output=True)
    for t in threads:
        t.join(timeout=10)

    with open(events_file, "rb") as f:
        f.seek(offset)
        for line in f.read().decode("utf-8", "replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("t", 0) >= t0:
                e.pop("seq", None)
                collected.append(e)
    collected.sort(key=lambda e: float(e["t"]))  # type: ignore[arg-type]
    meta = {
        "type": "meta",
        "label": "Recorded session",
        "recorded": time.strftime("%Y-%m-%d %H:%M"),
        "guest": "QNX 8.0 x86_64 in QEMU 11.1.3, TCG",
        "how": "scripts/record_session.py against a guest started by scripts/run_guest.sh start",
        "samples": SAMPLES,
        "story": [{"at": at, "what": say} for at, say, _ in STORY],
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for e in [meta, *collected]:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    kinds: dict[str, int] = {}
    for e in collected:
        kinds[str(e["type"])] = kinds.get(str(e["type"]), 0) + 1
    print(f"wrote {out}: {kinds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
