#!/usr/bin/env python3
"""Render the console-facelift handoff mock once per Tweak state, as PNGs.

The mock (`Console Facelift.dc.html` and its `support.js`) lives beside the plan in the vault,
not in this repo. Its four Tweaks are template flags -- `selected`/`unselected`,
`keybarCompact`, `showF11` -- so each state is pinned by rewriting those flags in a scratch copy,
and the copy is screenshotted by headless Chromium from the Playwright browser cache. No
Playwright package is needed; only its downloaded browser.

    scripts/render-handoff-mock.py [--mock DIR] [--out DIR]

Exits 0 having written one PNG per state, or non-zero naming what was missing.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_MOCK = Path(
    "/mnt/vault/Portfolio/infra/remote-agents/plans/2026-09-25-console-facelift-handoff"
)
_PAGE = "Console Facelift.dc.html"

#: Each Tweak state as the four flags' values. The first is the mock's own default.
STATES: dict[str, dict[str, str]] = {
    "selected-full-f11": {"selected": "true", "unselected": "false", "keybarCompact": "false",
                          "showF11": "true"},
    "unselected-full-f11": {"selected": "false", "unselected": "true",
                            "keybarCompact": "false", "showF11": "true"},
    "selected-compact": {"selected": "true", "unselected": "false", "keybarCompact": "true",
                         "showF11": "true"},
    "selected-full-no-f11": {"selected": "true", "unselected": "false",
                             "keybarCompact": "false", "showF11": "false"},
}  # fmt: skip


def _chromium() -> Path:
    candidates = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    if not candidates:
        sys.exit("no Chromium in ~/.cache/ms-playwright; run `playwright install chromium`")
    return candidates[-1]


def pinned(page: str, flags: dict[str, str]) -> str:
    """The page with each flag's `{{ name }}` replaced by its value for this state."""
    for name, value in flags.items():
        page = page.replace("{{ " + name + " }}", "{{ " + value + " }}")
    return page


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mock", type=Path, default=_MOCK)
    parser.add_argument("--out", type=Path, default=Path("build/facelift-captures/mock"))
    arguments = parser.parse_args()
    source = arguments.mock / _PAGE
    if not source.is_file():
        sys.exit(f"the mock is not at {source}")
    chromium = _chromium()
    arguments.out.mkdir(parents=True, exist_ok=True)
    page = source.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as scratch:
        shutil.copy(arguments.mock / "support.js", scratch)
        for state, flags in STATES.items():
            html = Path(scratch) / f"{state}.html"
            html.write_text(pinned(page, flags), encoding="utf-8")
            png = arguments.out.resolve() / f"{state}.png"
            subprocess.run(
                [
                    str(chromium), "--headless=new", "--no-sandbox", "--disable-gpu",
                    "--hide-scrollbars", "--window-size=1700,2400",
                    f"--screenshot={png}", "--virtual-time-budget=3000", html.as_uri(),
                ],
                check=True, capture_output=True, timeout=60,
            )  # fmt: skip
            if not png.is_file() or png.stat().st_size == 0:
                sys.exit(f"Chromium wrote no screenshot for {state}")
            print(png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
