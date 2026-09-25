"""Live drill: the real TUI's feed holds still, an arrival takes the cursor, limits keep columns.

The 0.46.0 fixes are pinned headless in `tests/unit/adapters/tui`. What those cannot show is
the real program in a real terminal: a Textual app on a tmux pane, redrawing on its own timers
and on the store watcher, captured the way the owner sees it.

**Not opt-in, unlike its neighbours.** It drives its own scratch tmux server against a
fabricated HOME, so it reads and writes nothing of the owner's, and it is a stage gate's
command: an opt-in skip there would report green over a drill that never ran. It skips only
where tmux itself is missing, and says so as BLOCKED.

The observation is appended to the durable table, not spooled: the TUI reads
`agent_activity` and never drains the spool (the service does), so with no service running a
spooled record would never reach the pane. `test_three_pane_console` lands its observation the
same way.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.domain.models import SessionId

#: A background-colour SGR in either spelling: what marks the highlighted option.
_BACKGROUND = re.compile(r"\x1b\[[0-9;]*?48;[52];[0-9;]*m")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PYTHON = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"

_REGISTRY = """version: 1
projects:
  - path: {project}
    name: qualification
    area: infra
    enabled: true
    added: 2026-09-22
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


async def _append(home: Path, activities) -> None:
    from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS

    connection = open_database(
        home / ".local" / "state" / "remote-agents" / "sessions.sqlite3", migrations=MIGRATIONS
    )
    try:
        store = SQLiteActivityStore(connection)
        for activity in activities:
            await store.append(activity)
    finally:
        connection.close()


def _observation(kind_name: str, detail: str, observed_at: datetime):
    from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity

    return AgentActivity(
        str(SessionId.new()),
        ActivityKind[kind_name],
        detail,
        observed_at,
        ActivityConfidence.REPORTED,
    )


async def _run(*argv: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{argv} failed: {err.decode()}")
    return out.decode()


async def _capture(socket: str, *, escapes: bool = False) -> list[str]:
    argv = ["tmux", "-L", socket, "capture-pane", "-p", "-t", "tui:"]
    if escapes:
        argv.insert(4, "-e")
    return (await _run(*argv)).splitlines()


def _region(lines: list[str], title: str) -> list[str]:
    """The lines inside the bordered pane whose top border carries `title`."""
    top = next(index for index, line in enumerate(lines) if title in _ANSI.sub("", line))
    column = _ANSI.sub("", lines[top]).index("╭")
    body = []
    for line in lines[top + 1 :]:
        plain = _ANSI.sub("", line)
        if plain[column : column + 1] == "╰":
            break
        body.append(line)
    return body


def _inner(line: str) -> str:
    """The cells between a right-hand pane's two borders, escapes kept if `line` had them."""
    return line.split("│")[-2] if line.count("│") >= 2 else ""


def _feed_plain(lines: list[str]) -> list[str]:
    return [_ANSI.sub("", _inner(line)) for line in _region(lines, "Feed ·")]


async def _until(predicate, timeout: float, step: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = await predicate()
        if result or time.monotonic() > deadline:
            return result
        await asyncio.sleep(step)


async def test_the_feed_holds_still_an_arrival_takes_the_cursor_and_limits_keep_columns(
    tmp_path: Path,
) -> None:
    if shutil.which("tmux") is None:
        pytest.skip("BLOCKED: tmux is not installed")

    home = _fabricated_home(tmp_path)
    # Hours old, so no age ticks over between the two still captures.
    earlier = datetime.now(UTC) - timedelta(hours=3)
    await _append(
        home,
        [
            _observation("COMPLETED", f"older answer {index}", earlier - timedelta(minutes=index))
            for index in range(6)
        ],
    )
    socket = f"remote-agents-test-{SessionId.new().value.hex}"
    try:
        await _run(
            "tmux", "-L", socket, "new-session", "-d", "-s", "tui", "-x", "140", "-y", "44",
            "env", f"HOME={home}", str(_PYTHON), "-m", "remote_agents", "tui",
        )  # fmt: skip

        async def drawn() -> bool:
            text = "\n".join(await _capture(socket))
            return "Plan limits" in text and "✓ finished" in text

        assert await _until(drawn, 40.0), "\n".join(await _capture(socket))
        # Let the mount-time reloads (limits, feed, sessions) all land before the still pair.
        await asyncio.sleep(3.0)

        # 1. Nothing new for twelve seconds: the feed rows are byte-identical.
        first = _feed_plain(await _capture(socket))
        await asyncio.sleep(12.0)
        second = _feed_plain(await _capture(socket))
        assert first == second, "\n".join(["--- first", *first, "--- second", *second])

        # 2. One arrival: it becomes the top row, and the highlighted one.
        await _append(home, [_observation("NEEDS_ANSWER", "May I push?", datetime.now(UTC))])

        async def arrived() -> list[str] | None:
            lines = await _capture(socket, escapes=True)
            feed = [_inner(line) for line in _region(lines, "Feed ·")]
            return feed if feed and "needs answer" in _ANSI.sub("", feed[0]) else None

        feed = await _until(arrived, 20.0)
        assert feed, "\n".join(_feed_plain(await _capture(socket)))
        backgrounds = [bool(_BACKGROUND.search(row)) for row in feed if _ANSI.sub("", row).strip()]
        assert backgrounds[0], "the arrival is not the highlighted row"
        assert not any(backgrounds[1:]), f"more than one row is highlighted: {backgrounds}"

        # 3. Every limits row draws `5h` then `wk`, whatever it read (here: nothing). On the
        #    one-line layout the header row names the columns once (DEC-106), so there the
        #    header carries `5h` then `week` and every row carries both bars under them.
        limits = [
            _ANSI.sub("", _inner(line)) for line in _region(await _capture(socket), "Plan limits")
        ]
        text = "\n".join(limits)
        header = next((line for line in limits if "expected" in line.split()), None)
        if header is not None:
            assert header.index("5h") < header.index("week"), text
            rows = [line for line in limits if line.split()[:1] in (["claude"], ["codex"])]
            assert rows, text
            for row in rows:
                bars = [match.start() for match in re.finditer(r"[█░┃]+", row)]
                assert bars == [header.index("5h"), header.index("week")], f"{row}\n{text}"
            return
        # Each agent's own block: from its name to the next line that starts a new row (a name
        # or a Remote Control line), so one agent's labels cannot satisfy another's check.
        starts = [i for i, line in enumerate(limits) if line.strip() and not line.startswith("  ")]
        blocks = {
            limits[start].split()[0]: "\n".join(limits[start:end])
            for start, end in zip(starts, [*starts[1:], len(limits)], strict=True)
        }
        agents = [name for name in ("claude", "codex") if name in blocks]
        assert agents, text
        for agent in agents:
            block = blocks[agent]
            assert "5h" in block and "wk" in block, f"{agent}:\n{text}"
            assert block.index("5h") < block.index("wk"), f"{agent}:\n{text}"
    finally:
        try:
            await _run("tmux", "-L", socket, "kill-server")
        except RuntimeError:
            pass
