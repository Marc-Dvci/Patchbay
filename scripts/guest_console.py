"""Run shell commands on the guest's serial console (127.0.0.1:5530) and print what comes back.

    python3 scripts/guest_console.py "pidin ar" "ls /dev/i2c*"

For when ssh is not up yet. Each command gets `--wait` seconds (default 3) before the next one is sent.
"""

from __future__ import annotations

import argparse
import socket
import time


def run(host: str, port: int, commands: list[str], wait: float) -> str:
    out = bytearray()
    with socket.create_connection((host, port), timeout=5) as s:
        s.settimeout(0.2)

        def drain(seconds: float) -> None:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    chunk = s.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    return
                out.extend(chunk)

        s.sendall(b"\n")
        drain(0.5)
        for cmd in commands:
            s.sendall(cmd.encode() + b"\n")
            drain(wait)
    return out.decode("utf-8", "replace")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("commands", nargs="+")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5530)
    p.add_argument("--wait", type=float, default=3.0)
    a = p.parse_args()
    print(run(a.host, a.port, a.commands, a.wait))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
