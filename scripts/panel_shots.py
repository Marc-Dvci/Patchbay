"""Screenshots and a short GIF of the patch panel replaying the committed recorded session.

    python3 scripts/panel_shots.py            writes docs/panel-1440.png, docs/panel-390.png, docs/panel.gif

The page is the real one, served by patchbay.panel from the recording; the shots are taken when the replay reaches
the moment the BNO055's cord is pulled. The GIF needs ffmpeg.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from patchbay.clock import MonotonicClock
from patchbay.panel import DEFAULT_FIXTURE, PanelApp, RecordedSource, load_fixture, make_server

DOCS = Path(__file__).resolve().parents[1] / "docs"
PULLED = "document.querySelector('#caption').textContent.includes('unplug on BNO055')"


def main() -> int:
    meta, events = load_fixture(DEFAULT_FIXTURE)
    srv = make_server(PanelApp(RecordedSource(meta, events, MonotonicClock())), 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    vids = Path(tempfile.mkdtemp())
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height in ((1440, 1100), (390, 1500)):
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=2, bypass_csp=True)
            page.goto(url)
            page.wait_for_function(PULLED, timeout=120000, polling=200)
            page.wait_for_function("document.querySelectorAll('#wire-i2c li.bad').length > 2", timeout=30000)
            page.screenshot(path=str(DOCS / f"panel-{width}.png"), full_page=True)
            page.close()
        ctx = browser.new_context(
            viewport={"width": 1200, "height": 760},
            record_video_dir=str(vids),
            record_video_size={"width": 1200, "height": 760},
            bypass_csp=True,
        )
        page = ctx.new_page()
        page.goto(url)
        page.wait_for_function(PULLED, timeout=120000, polling=200)
        page.wait_for_function("document.querySelector('#caption').textContent.includes('cleared')", timeout=60000)
        ctx.close()
        browser.close()
    srv.shutdown()
    webm = next(vids.glob("*.webm"))
    if shutil.which("ffmpeg"):
        gif = DOCS / "panel.gif"
        # the last 12 seconds: the cord pulled, the sample failing, the cord back in
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-sseof",
                "-13",
                "-i",
                str(webm),
                "-vf",
                "fps=10,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];"
                "[b][p]paletteuse=dither=none",
                str(gif),
            ],
            check=True,
        )
        print(f"{gif}: {gif.stat().st_size // 1024} KiB")
    print("screenshots in docs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
