"""A harmless fake agent that draws, after a resume, the three first looks Codex 0.170 was measured
drawing (`docs/acceptance-2026-11-surviving-codex-turn.md`): a surviving turn's on-screen busy
line, a boot's title spinner over an idle composer, or an idle composer at once.

Run as a script: `python fake_codex_screens.py --resume <conversation>`. The conversation names
the first look -- one holding `survivor`, `boot` or neither. It prints `READY` (the launch's
readiness marker), draws that look, then echoes each line it is given as `GOT: <line>` under a
one-line composer (a rule, then `❯ `), so whatever was typed is read off the pane itself.
"""

from __future__ import annotations

import sys
import time

#: How long the surviving turn keeps its busy line up, and the boot its title spinner.
SURVIVOR_SECONDS = 3.0
BOOT_SECONDS = 5.0
#: Codex 0.170.0's busy line at a survivor's first look, its timer counting from the resume.
BUSY_LINE = "• Working (3s • esc to interrupt) · 1 background terminal running · /ps to view…"
SPINNER = "⠋ fake-codex"
TITLE = "fake-codex"


def _title(text: str) -> None:
    sys.stdout.write(f"\033]2;{text}\033\\")
    sys.stdout.flush()


def _composer() -> None:
    sys.stdout.write("─" * 24 + "\n❯ ")
    sys.stdout.flush()


def main(argv: list[str]) -> None:
    look = argv[argv.index("--resume") + 1] if "--resume" in argv else "fresh"
    _title(TITLE)
    print("READY", flush=True)
    if "survivor" in look:
        print(BUSY_LINE, flush=True)
        _composer()
        time.sleep(SURVIVOR_SECONDS)
        # The turn ends: the busy line goes, and the composer is drawn again, idle.
        sys.stdout.write("\033[2J\033[H• ok\n")
        _composer()
    elif "boot" in look:
        _title(SPINNER)
        _composer()
        time.sleep(BOOT_SECONDS)
        _title(TITLE)
    else:
        _composer()
    while True:
        try:
            line = input()
        except EOFError:
            break
        print("GOT: " + line, flush=True)
        _composer()


if __name__ == "__main__":
    main(sys.argv)
