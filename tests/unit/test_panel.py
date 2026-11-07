"""The patch panel's HTTP surface, through PanelApp.handle (no socket), with a fake clock."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from patchbay.clock import FakeClock
from patchbay.panel import DEFAULT_FIXTURE, LiveSource, PanelApp, RecordedSource, load_fixture

FIXTURE = [
    {"type": "meta", "label": "Recorded session", "recorded": "2026-11-07 03:40", "guest": "test"},
    {"type": "i2c", "t": 100.0, "segs": [], "ok": True, "what": "BNO055 @0x28 CHIP_ID = 0xA0"},
    {"type": "sample", "t": 100.5, "name": "imu-bno055", "line": "BNO055 Chip ID: 0xA0"},
    {"type": "fault", "t": 101.0, "action": "add", "fault": {"kind": "unplug", "bus": "i2c", "target": 40}},
    {"type": "fault", "t": 103.0, "action": "clear", "bus": None, "cleared": 1},
]


def write_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "s.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for e in FIXTURE:
            f.write(json.dumps(e) + "\n")
    return p


def recorded_app(tmp_path: Path) -> tuple[FakeClock, PanelApp]:
    meta, events = load_fixture(write_fixture(tmp_path))
    clock = FakeClock(1000.0)
    return clock, PanelApp(RecordedSource(meta, events, clock))


def get(app: PanelApp, target: str) -> tuple[int, dict[str, object]]:
    status, _, body = app.handle("GET", target)
    return status, json.loads(body)


def test_loading_a_fixture_puts_times_relative_to_the_first_event(tmp_path: Path) -> None:
    meta, events = load_fixture(write_fixture(tmp_path))
    assert meta["label"] == "Recorded session"
    assert [e["t"] for e in events] == [0.0, 0.5, 1.0, 3.0]


def test_the_state_says_recorded_and_disables_faults(tmp_path: Path) -> None:
    _, app = recorded_app(tmp_path)
    status, s = get(app, "/api/state")
    assert status == 200 and s["mode"] == "recorded" and s["faults_enabled"] is False
    assert {c["id"] for c in s["topology"]["chips"]} == {"bno055", "sht3x", "mcp3008"}  # type: ignore[index]


def test_a_replay_releases_events_at_the_speed_they_were_recorded(tmp_path: Path) -> None:
    clock, app = recorded_app(tmp_path)
    _, r = get(app, "/api/events?since=0")
    assert [e["type"] for e in r["events"]] == ["i2c"] and r["next"] == 1  # type: ignore[index]
    clock.advance(0.9)
    _, r = get(app, "/api/events?since=1")
    assert [e["type"] for e in r["events"]] == ["sample"] and r["next"] == 2  # type: ignore[index]
    clock.advance(2.5)
    _, r = get(app, "/api/events?since=2")
    assert [e["action"] for e in r["events"]] == ["add", "clear"]  # type: ignore[index]


def test_a_replay_loops_with_new_sequence_numbers_and_a_lap_count(tmp_path: Path) -> None:
    clock, app = recorded_app(tmp_path)
    clock.advance(5.0 + 0.1)  # 3 s of events plus the 2 s pause: lap 1 has begun
    _, r = get(app, "/api/events?since=4")
    assert r["next"] == 5
    assert r["events"][0]["seq"] == 5 and r["events"][0]["lap"] == 1  # type: ignore[index]


def test_faults_are_refused_in_a_recorded_session(tmp_path: Path) -> None:
    _, app = recorded_app(tmp_path)
    status, _, body = app.handle("POST", "/api/fault", b'{"kind":"nak","bus":"i2c","target":40}')
    assert status == 409 and b"recorded" in body


def test_a_bad_since_value_is_a_400(tmp_path: Path) -> None:
    _, app = recorded_app(tmp_path)
    assert get(app, "/api/events?since=abc")[0] == 400


def test_the_page_and_its_assets_are_served_and_paths_cannot_escape(tmp_path: Path) -> None:
    _, app = recorded_app(tmp_path)
    status, ctype, body = app.handle("GET", "/")
    assert status == 200 and ctype.startswith("text/html") and b"PATCHBAY" in body
    assert app.handle("GET", "/panel.js")[0] == 200
    assert app.handle("GET", "/../panel.py")[0] == 404
    assert app.handle("GET", "/%2e%2e/panel.py")[0] == 404


def live_app(tmp_path: Path, sent: list[tuple[str, dict[str, object]]]) -> tuple[Path, Path, PanelApp]:
    ev = tmp_path / "events.jsonl"
    samples = tmp_path / "samples"
    samples.mkdir()

    def send(path: str, req: dict[str, object]) -> dict[str, object]:
        sent.append((path, req))
        return {"ok": True}

    return ev, samples, PanelApp(LiveSource(ev, samples, FakeClock(5.0)), "ctl.sock", send=send)


def test_live_events_and_sample_lines_are_merged_as_the_files_grow(tmp_path: Path) -> None:
    ev, samples, app = live_app(tmp_path, [])
    ev.write_text(json.dumps({"seq": 9, "t": 1.0, "type": "i2c", "ok": True, "what": "x"}) + "\n")
    (samples / "imu-bno055.log").write_bytes(b"BNO055 Chip ID: 0xA0\rCalib")
    _, r = get(app, "/api/events?since=0")
    assert [(e["type"], e["seq"]) for e in r["events"]] == [("i2c", 1), ("sample", 2)]  # type: ignore[index]
    with open(samples / "imu-bno055.log", "ab") as f:
        f.write(b"ration done\n")
    _, r = get(app, "/api/events?since=2")
    assert [e["line"] for e in r["events"]] == ["Calibration done"]  # type: ignore[index]


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            {"kind": "unplug", "bus": "i2c", "target": 0x28},
            {"cmd": "fault", "kind": "unplug", "bus": "i2c", "target": 40},
        ),
        (
            {"kind": "delay", "bus": "spi", "delay_ms": 1500, "count": 2},
            {"cmd": "fault", "kind": "delay", "bus": "spi", "count": 2, "delay_ms": 1500},
        ),
    ],
)
def test_live_fault_buttons_send_the_control_request_the_fault_matrix_sends(
    tmp_path: Path, body: dict[str, object], expected: dict[str, object]
) -> None:
    sent: list[tuple[str, dict[str, object]]] = []
    _, _, app = live_app(tmp_path, sent)
    status, _, _ = app.handle("POST", "/api/fault", json.dumps(body).encode())
    assert status == 200 and sent == [("ctl.sock", expected)]


@pytest.mark.parametrize(
    "body",
    [
        b'{"kind":"melt","bus":"i2c"}',
        b'{"kind":"nak","bus":"can"}',
        b'{"kind":"nak","bus":"i2c","target":300}',
        b'{"kind":"delay","bus":"i2c","delay_ms":0}',
        b'{"kind":"nak","bus":"i2c","count":"lots"}',
        b"[1,2]",
        b"not json",
    ],
)
def test_malformed_fault_requests_never_reach_the_back_end(tmp_path: Path, body: bytes) -> None:
    sent: list[tuple[str, dict[str, object]]] = []
    _, _, app = live_app(tmp_path, sent)
    assert app.handle("POST", "/api/fault", body)[0] == 400
    assert sent == []


def test_clear_can_name_one_bus(tmp_path: Path) -> None:
    sent: list[tuple[str, dict[str, object]]] = []
    _, _, app = live_app(tmp_path, sent)
    app.handle("POST", "/api/clear", b'{"bus":"spi"}')
    assert sent == [("ctl.sock", {"cmd": "clear", "bus": "spi"})]


@pytest.mark.skipif(not DEFAULT_FIXTURE.exists(), reason="the recorded session has not been committed yet")
def test_the_committed_recording_replays_bus_traffic_faults_and_all_three_samples() -> None:
    meta, events = load_fixture(DEFAULT_FIXTURE)
    assert "QEMU" in str(meta["guest"])
    kinds = {e["type"] for e in events}
    assert {"i2c", "spi", "fault", "sample", "note"} <= kinds
    assert {e["name"] for e in events if e["type"] == "sample"} == {
        "imu-bno055",
        "temp-humidity-sht3x",
        "mcp-3008-joystick",
    }
    assert any("CHIP_ID = 0xA0" in str(e.get("what")) for e in events)
    assert any(e["type"] == "fault" and e.get("fault", {}).get("kind") == "unplug" for e in events)
