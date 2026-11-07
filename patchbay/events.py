"""Structured events from the back end: one JSON object per line, numbered, with the host's monotonic time.

  {"seq": 12, "t": 1534.2, "type": "i2c", "segs": [...], "ok": true, "what": "BNO055 @0x28 CHIP_ID = 0xA0"}
  {"seq": 13, "t": 1534.3, "type": "spi", "cs": 0, "tx": "018000", "rx": "00026a", "ok": true, "what": "..."}
  {"seq": 14, "t": 1535.0, "type": "fault", "action": "add", "fault": {...}}
  {"seq": 15, "t": 1540.0, "type": "sample", "name": "imu-bno055", "line": "BNO055 Chip ID: 0xA0"}

The back end writes bus and fault events; scripts/record_session.py adds the guest's sample output lines. The panel
reads the same objects live (from the file) or from a recorded session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from .clock import Clock, MonotonicClock


class EventLog:
    def __init__(self, path: str | Path | None = None, clock: Clock | None = None) -> None:
        self.clock = clock or MonotonicClock()
        self.seq = 0
        self.file: TextIO | None = open(path, "a", encoding="utf-8") if path else None  # noqa: SIM115
        self.memory: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        e = {"seq": self.seq, "t": round(self.clock.now(), 4), **event}
        if self.file is not None:
            self.file.write(json.dumps(e, ensure_ascii=False) + "\n")
            self.file.flush()
        else:
            self.memory.append(e)
        return e
