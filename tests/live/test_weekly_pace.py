"""Live drill: the real TUI draws weekly pace in a real terminal (DEC-106).

The pace math and layout are pinned headless in `tests/unit`. What those cannot show is the
real program on a tmux pane, reading a real `claude-limits.json` the way the status-line hop
writes it, at the two sizes the handoff names: 200x50, where the limits pane is wide and the
pace is two columns, and 100x30, where it stacks and the pace is a line under the week bar.

**Not opt-in**, for `test_steady_panes`' reason: it drives its own scratch tmux server against
a fabricated HOME, reads and writes nothing of the owner's, and is a stage gate's command. It
skips only where tmux itself is missing, and says so as BLOCKED.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.domain.models import SessionId

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PYTHON = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"
_BAR = re.compile(r"[█░┃]+")

_REGISTRY = """version: 1
projects:
  - path: {project}
    name: qualification
    area: infra
    enabled: true
    added: 2026-09-25
"""


def _fabricated_home(root: Path) -> Path:
    home = root / "home"
    project = home / "dev" / "infra" / "qualification"
    project.mkdir(parents=True)
    registry = home / "projects-registry.yaml"
    registry.write_text(_REGISTRY.format(project=project), encoding="utf-8")
    config = home / ".config" / "remote-agents"
    config.mkdir(parents=True)
    state = home / ".local" / "state" / "remote-agents"
    state.mkdir(parents=True)
    (config / "config.toml").write_text(
        f'[paths]\ndev_root = "{home / "dev"}"\n'
        f'registry_path = "{registry}"\n'
        f'database_path = "{state / "sessions.sqlite3"}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )
    return home


def _write_claude_limits(home: Path) -> None:
    """What the status-line hop records: Claude Code's own stdin shape, dated now.

    The week resets in six days and an hour, so it reads `↻ 6d` for the whole drill (`until`
    rounds down), and 23 hours of it have gone: an even spend would be at round(13.7) = 14%.
    """
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


async def _run(*argv: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{argv} failed: {err.decode()}")
    return out.decode()


async def _capture(socket: str) -> list[str]:
    return (await _run("tmux", "-L", socket, "capture-pane", "-p", "-t", "tui:")).splitlines()


def _limits(lines: list[str]) -> list[str]:
    """The Plan limits pane's inner lines, between its two side borders."""
    plain = [_ANSI.sub("", line) for line in lines]
    top = next(index for index, line in enumerate(plain) if "Plan limits" in line)
    column = plain[top].index("╭")
    body = []
    for line in plain[top + 1 :]:
        if line[column : column + 1] == "╰":
            break
        body.append(line[column + 1 :].rsplit("│", 1)[0])
    return body


async def _until(predicate, timeout: float, step: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = await predicate()
        if result or time.monotonic() > deadline:
            return result
        await asyncio.sleep(step)


async def _drawn_limits(tmp_path: Path, width: int, height: int) -> list[str]:
    home = _fabricated_home(tmp_path / f"{width}x{height}")
    _write_claude_limits(home)
    socket = f"remote-agents-test-{SessionId.new().value.hex}"
    try:
        await _run(
            "tmux", "-L", socket, "new-session", "-d", "-s", "tui",
            "-x", str(width), "-y", str(height),
            "env", f"HOME={home}", str(_PYTHON), "-m", "remote_agents", "tui",
        )  # fmt: skip

        async def paced() -> list[str] | None:
            try:
                limits = _limits(await _capture(socket))
            except StopIteration:
                return None
            claude = [line for line in limits if line.split()[:1] == ["claude"]]
            return (
                limits
                if any(" 7%" in line for line in claude)
                or any(" 7%" in line for line in limits if " wk " in line)
                else None
            )

        limits = await _until(paced, 40.0)
        assert limits, "\n".join(await _capture(socket))
        return limits
    finally:
        try:
            await _run("tmux", "-L", socket, "kill-server")
        except RuntimeError:
            pass


@pytest.fixture(autouse=True)
def _tmux() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("BLOCKED: tmux is not installed")


async def test_a_wide_limits_pane_draws_expected_and_the_tick_at_200x50(tmp_path: Path) -> None:
    limits = await _drawn_limits(tmp_path, 200, 50)
    text = "\n".join(limits)
    header = next(line for line in limits if "expected" in line)
    claude = next(line for line in limits if line.split()[:1] == ["claude"])

    # 14% sits in the `expected` column: right-aligned under its heading.
    end = header.index("expected") + len("expected")
    assert claude[end - 3 : end] == "14%", text
    assert "▼ 7 under" in claude, text

    # The tick sits inside the week bar at round(14/100 * 16) = cell 2.
    week = [match for match in _BAR.finditer(claude)][1]
    assert len(week.group()) == 16, text
    assert week.group().index("┃") == 2, text
    assert "claude · status line · live" in text, text


async def test_a_stacked_limits_pane_draws_the_pace_line_at_100x30(tmp_path: Path) -> None:
    limits = await _drawn_limits(tmp_path, 100, 30)
    text = "\n".join(limits)
    assert "expected" not in text, f"the pane should stack at 100x30:\n{text}"
    week = next(index for index, line in enumerate(limits) if " wk " in line)
    assert "┃" in limits[week], text
    assert "↻" not in limits[week], text
    assert "↻ 6d · exp 14%  ▼ 7 under" in limits[week + 1], text
