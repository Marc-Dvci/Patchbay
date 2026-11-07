"""The panel in a real browser (Playwright, headless Chromium): accessibility with axe-core, and the keyboard path
from a fault button to the control request. Needs `pip install playwright`, `playwright install chromium`, and
axe-core's axe.min.js at $AXE_JS; skipped otherwise, with the reason printed. The page's Content-Security-Policy
forbids eval, which Playwright's wait_for_function and axe need, so the test browser bypasses it."""

from __future__ import annotations

import gzip
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from patchbay.clock import FakeClock, MonotonicClock
from patchbay.panel import DEFAULT_FIXTURE, LiveSource, PanelApp, RecordedSource, load_fixture, make_server

pytestmark = pytest.mark.e2e
playwright = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
AXE = Path(os.environ.get("AXE_JS", Path.home() / "Patchbay-work" / "js" / "axe.min.js"))


def serve(app: PanelApp) -> Iterator[str]:
    srv = make_server(app, 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


@pytest.fixture
def recorded_url(tmp_path: Path) -> Iterator[str]:
    if DEFAULT_FIXTURE.exists():
        meta, events = load_fixture(DEFAULT_FIXTURE)
    else:
        p = tmp_path / "s.jsonl.gz"
        with gzip.open(p, "wt") as f:
            f.write(json.dumps({"type": "meta", "recorded": "test", "guest": "test"}) + "\n")
            f.write(json.dumps({"type": "sample", "t": 0, "name": "imu-bno055", "line": "BNO055 Chip ID: 0xA0"}) + "\n")
        meta, events = load_fixture(p)
    yield from serve(PanelApp(RecordedSource(meta, events, MonotonicClock())))


@pytest.fixture
def live_url(tmp_path: Path) -> Iterator[tuple[str, list[dict[str, object]]]]:
    sent: list[dict[str, object]] = []

    def send(path: str, req: dict[str, object]) -> dict[str, object]:
        sent.append(req)
        return {"ok": True}

    (tmp_path / "events.jsonl").write_text("")
    app = PanelApp(LiveSource(tmp_path / "events.jsonl", None, FakeClock()), "ctl.sock", send=send)
    for url in serve(app):
        yield url, sent


@pytest.mark.parametrize("width", [1440, 390])
def test_the_recorded_panel_has_no_wcag_2_2_aa_violations(recorded_url: str, width: int) -> None:
    if not AXE.exists():
        pytest.skip(f"axe-core not found at {AXE}")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900}, bypass_csp=True)
        page.goto(recorded_url)
        page.wait_for_function("document.querySelectorAll('.tape li').length > 0", timeout=15000)
        page.add_script_tag(path=str(AXE))
        result = page.evaluate(
            "axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}})"
        )
        browser.close()
    violations = [(v["id"], v["help"], len(v["nodes"])) for v in result["violations"]]
    assert violations == []


def test_the_recorded_panel_says_recorded_and_its_buttons_are_disabled(recorded_url: str) -> None:
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(bypass_csp=True)
        page.goto(recorded_url)
        page.wait_for_selector("#ctl button")
        assert "RECORDED SESSION" in page.inner_text("#mode")
        assert page.eval_on_selector_all("#ctl button", "bs => bs.every(b => b.disabled)")
        browser.close()


def test_a_fault_button_works_from_the_keyboard(live_url: tuple[str, list[dict[str, object]]]) -> None:
    url, sent = live_url
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(bypass_csp=True)
        page.goto(url)
        page.wait_for_selector("#ctl button:not([disabled])")
        for _ in range(40):
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement.textContent") == "Pull cord":
                break
        assert page.evaluate("document.activeElement.textContent") == "Pull cord"
        with page.expect_response(lambda r: r.url.endswith("/api/fault")):
            page.keyboard.press("Enter")
        browser.close()
    assert {"cmd": "fault", "kind": "unplug", "bus": "i2c", "target": 0x28} in sent
