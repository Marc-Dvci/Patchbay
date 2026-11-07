"""The control socket's client: one JSON request, one JSON answer. `python -m patchbay ctl`, the fault matrix and the
panel's fault buttons all inject faults through this function."""

from __future__ import annotations

import json
import socket


def control(path: str, request: dict[str, object], timeout: float = 5.0) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)  # type: ignore[no-any-return]
