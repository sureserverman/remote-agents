"""The console facelift on a real console: the cell budget, one key row, pace inside the chrome.

A private `remote-agents` console (see `tests/support/console_drill.py`) is entered from a bare
shell in an outer tmux pane of the size under test, and its panes are measured and captured.
Sub-plan 3 of the console-facelift master plan; the budget is the handoff README's.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from console_drill import Drill, drill_console, wait

from remote_agents.adapters.tmux.codec import CONSOLE_SESSION_NAME


def _write_claude_limits(home: Path) -> None:
    """A live Claude reading, the status-line hop's own shape (as `test_weekly_pace.py`)."""
    now = datetime.now(UTC)
    document = {
        "rate_limits": {
            "five_hour": {
                "used_percentage": 28,
                "resets_at": int((now + timedelta(hours=1)).timestamp()),
            },
            "seven_day": {
                "used_percentage": 7,
                "resets_at": int((now + timedelta(days=6, hours=1)).timestamp()),
            },
        },
        "recorded_at": int(now.timestamp()),
    }
    path = home / ".local" / "state" / "remote-agents" / "claude-limits.json"
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture(scope="module")
def console() -> Iterator[Drill]:
    with drill_console(200, 50, prepare=_write_claude_limits) as running:
        running.add_session()
        yield running


def _geometry(console: Drill) -> dict[str, tuple[int, int]]:
    """Each console pane's width and height, by slot."""
    lines = console.inner(
        "list-panes", "-t", f"{CONSOLE_SESSION_NAME}:", "-F",
        "#{@remote_agents_console_slot} #{pane_width} #{pane_height}",
    ).split("\n")  # fmt: skip
    found = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 3:
            found[parts[0]] = (int(parts[1]), int(parts[2]))
    return found


def _limits_screen(console: Drill) -> list[str]:
    pane = next(
        line.split()[0]
        for line in console.inner(
            "list-panes",
            "-t",
            f"{CONSOLE_SESSION_NAME}:",
            "-F",
            "#{pane_id} #{@remote_agents_console_slot}",
        ).splitlines()  # fmt: skip
        if line.endswith(" limits")
    )
    drawn = wait(
        lambda: console.inner("capture-pane", "-p", "-t", pane).strip() or None, bound=10.0
    )
    return (drawn or "").split("\n")


def _settled(console: Drill, width: int, height: int) -> dict[str, tuple[int, int]]:
    """The geometry once the layout hooks have run *and* the limits pane has fitted itself.

    Stable-for-a-second alone is not settled: straight after start the limits process may not
    have drawn yet, and a pane that has not drawn holds still at whatever share the layout gave
    it. Measured: one run in four read 15 limits rows that way, where the pane fits to 7 once
    it has drawn. So the pane must show its stamp line first, then hold still.
    """
    console.resize(width, height)

    def drawn() -> bool:
        return "live" in "\n".join(_limits_screen(console))

    assert wait(drawn, bound=30.0), _limits_screen(console)

    def stable() -> dict[str, tuple[int, int]] | None:
        first = _geometry(console)
        time.sleep(1.5)
        second = _geometry(console)
        return second if first == second and "limits" in second else None

    found = wait(stable, bound=30.0)
    assert found, _geometry(console)
    return found


def test_cell_budget_at_200x50(console: Drill) -> None:
    """Left slot 119, right column 80, sessions 15 rows, limits exactly its content."""
    geometry = _settled(console, 200, 50)

    assert geometry["surface"][0] == 119, geometry
    assert geometry["sessions"][0] == geometry["limits"][0] == geometry["feed"][0] == 80, geometry
    assert geometry["sessions"][1] == 15, geometry
    limits = _limits_screen(console)
    assert limits[0].startswith("╭─ Plan limits") and limits[-1].startswith("╰"), limits
    assert not any(line.strip("│ ") == "" for line in limits[1:-1][-1:]), (
        f"the limits pane ends in a blank row, so it is taller than what it draws: {limits}"
    )


def test_cell_budget_at_100x30(console: Drill) -> None:
    """At 100 columns the limits pane stacks, and every stacked line is on screen."""
    geometry = _settled(console, 100, 30)

    # 60% of 100 is a 59-cell slot, a divider, and 40 on the right; the README's "39" counts
    # the same split as 100 - 60 - 1.
    assert geometry["surface"][0] == 59, geometry
    assert geometry["sessions"][0] == 40, geometry
    limits = _limits_screen(console)
    assert limits[0].startswith("╭─ Plan limits") and limits[-1].startswith("╰"), limits
    drawn = "\n".join(limits)
    assert "claude" in drawn and "live" in drawn, (
        f"the limits pane is clipped: its stamp line is not on screen: {limits}"
    )
    console.resize(200, 50)


# --- captures beside the mock, and the two cross-plan checks (Task 2.4) ------------------------

_CAPTURES = Path(__file__).resolve().parents[2] / "build" / "facelift-captures" / "live"


def _capture(console: Drill, name: str) -> str:
    """The whole outer pane, bar included, written as text and as an SVG for the evaluator."""
    from rich.console import Console
    from rich.text import Text

    plain = console.screen()
    styled = console.screen(styled=True)
    _CAPTURES.mkdir(parents=True, exist_ok=True)
    (_CAPTURES / f"{name}.txt").write_text(plain, encoding="utf-8")
    width = max(len(line) for line in plain.splitlines())
    recorder = Console(record=True, width=width, file=open("/dev/null", "w"))  # noqa: SIM115
    recorder.print(Text.from_ansi(styled), end="")
    recorder.save_svg(str(_CAPTURES / f"{name}.svg"), title=f"ra-console {name}")
    return plain


def test_one_key_row(console: Drill) -> None:
    """In the 200x50 capture the function keys are a row once, on the bar, and nowhere else."""
    _settled(console, 200, 50)
    screen = wait(
        lambda: (lambda s: s if "Plan limits" in s and "12 projects" in s else None)(
            console.screen()
        ),
        bound=20.0,
    )
    assert screen, console.screen()
    _capture(console, "200x50")
    rows = screen.rstrip("\n").split("\n")
    key_rows = [
        index
        for index, row in enumerate(rows)
        if "help" in row and "settings" in row and "projects" in row
    ]
    assert key_rows == [len(rows) - 1], (key_rows, len(rows))
    assert not any("F3 F4" in row or "^r refresh" in row for row in rows), screen


def test_pace_survives_chrome(console: Drill) -> None:
    """The limits pane, inside its border, still shows its header, `expected`, `vs pace`, and
    the source stamp."""
    _settled(console, 200, 50)
    limits = wait(
        lambda: (lambda lines: lines if "expected" in "\n".join(lines) else None)(
            _limits_screen(console)
        ),
        bound=20.0,
    )
    assert limits, _limits_screen(console)
    drawn = "\n".join(limits)
    assert limits[0].startswith("╭─ Plan limits"), limits
    assert "expected" in drawn and "vs pace" in drawn, limits
    assert "claude · status line" in drawn and "live" in drawn, limits


def test_the_narrow_console_is_captured(console: Drill) -> None:
    _settled(console, 100, 30)
    screen = wait(lambda: console.screen() if "12projects" in console.bar() else None, bound=20.0)
    assert screen, console.screen()
    _capture(console, "100x30")
    console.resize(200, 50)
