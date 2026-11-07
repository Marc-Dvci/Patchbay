"""Renders an SVG to a PNG at 2x on a white ground, with headless Chromium (Playwright).

python3 scripts/render_svg.py docs/patchbay-architecture.svg docs/patchbay-architecture.png
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    text = src.read_text(encoding="utf-8")
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (860, 560)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
        page.set_content(f'<html><body style="margin:0;background:#fff">{text}</body></html>')
        page.locator("svg").first.screenshot(path=str(out))
        browser.close()
    print(f"{out}: {w * 2} x {h * 2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
