"""A private `remote-agents` console, entered from a bare shell inside an outer tmux pane.

Shared by the live drills that need the real console window: its status bar, its panes and
their geometry. The console runs under a private `TMUX_TMPDIR`, so the owner's own console
(DEC-042's production socket) is never touched, and `drill_console` asserts that on the way out.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.codec import CONSOLE_SESSION_NAME, pane_mark_args
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_PYTHON = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"

_REGISTRY = """version: 1
projects:
  - path: {project}
    name: qualification
    area: infra
    enabled: true
    added: 2026-09-25
"""

#: What the drill may not change on the owner's own console.
#: What the drill could write to the owner's console if it leaked onto the production socket:
#: the bar's own options. Not the three published facts (selection, typing, Remote Control),
#: which the owner's own panes rewrite as they use the console, so a snapshot of them would
#: fail on the owner's activity rather than on a leak.
_OWNER_OPTIONS = re.compile(r"^status")


def _tmux(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=check, env=env
    ).stdout


def owner_console() -> tuple[str, ...]:
    """The owner's surface panes and bar options, read from the default socket dir."""
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX_TMPDIR", "TMUX"}}
    # The four surface panes by id, wherever they sit: an exchange moves them between windows
    # and opening a session adds panes, but neither restarts a surface. A leaked drill that
    # closed or rebuilt the console would.
    panes = "\n".join(
        sorted(
            line.split()[0]
            for line in _tmux(
                "-L",
                "remote-agents",
                "list-panes",
                "-a",
                "-F",
                "#{pane_id} #{pane_start_command}",
                env=env,
                check=False,
            ).splitlines()  # fmt: skip
            if "remote_agents pane " in line
        )
    )
    options = [
        line
        for scope in ((), ("-w",))
        for line in _tmux(
            "-L",
            "remote-agents",
            "show-options",
            *scope,
            "-t",
            f"{CONSOLE_SESSION_NAME}:",
            env=env,
            check=False,
        ).splitlines()  # fmt: skip
        if _OWNER_OPTIONS.match(line)
    ]
    return (panes, *options)


def fabricated_home(root: Path) -> Path:
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


async def record_session(home: Path, session_id: SessionId) -> None:
    """A RUNNING record, so the sessions pane has a row to select and rename."""
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS
    from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
    from remote_agents.domain.models import SessionDisplayIdentity, SessionRecord, SessionState

    connection = open_database(
        home / ".local" / "state" / "remote-agents" / "sessions.sqlite3", migrations=MIGRATIONS
    )
    try:
        await SQLiteSessionStore(connection).save(
            SessionRecord(
                session_id,
                ProjectId("qualification"),
                ProfileId("claude"),
                SessionDisplayIdentity("qualification", "claude", "regular", 1),
                SessionState.RUNNING,
                datetime.now(UTC),
            )
        )
    finally:
        connection.close()


def wait(check: Callable[[], object], bound: float = 40.0, step: float = 0.25):
    deadline = time.monotonic() + bound
    while True:
        found = check()
        if found or time.monotonic() > deadline:
            return found
        time.sleep(step)


class Drill:
    """A private `remote-agents` console, entered from a bare shell inside an outer pane."""

    def __init__(self, root: Path, home: Path, session_id: SessionId) -> None:
        self.root = root
        self.home = home
        self.session_id = session_id
        self.outer = f"remote-agents-test-bar-{session_id.value.hex}"
        self.env = {**os.environ, "TMUX_TMPDIR": str(root / "sock"), "HOME": str(home)}
        self.env.pop("TMUX", None)

    def inner(self, *args: str) -> str:
        return _tmux("-L", "remote-agents", *args, env=self.env)

    def start(self, width: int, height: int) -> None:
        (self.root / "sock").mkdir()
        # Isolation proven *before* any surface runs, not only by the teardown comparison: the
        # private server is started first and must answer from inside this drill's directory.
        self.inner("new-session", "-d", "-s", "drill-guard", "sleep", "600")
        socket_path = self.inner("display-message", "-p", "-t", "drill-guard:", "#{socket_path}")
        assert Path(socket_path.strip()).is_relative_to(self.root), socket_path
        entry = (
            f"env -u TMUX HOME={self.home} TMUX_TMPDIR={self.root / 'sock'} "
            f"{_PYTHON} -m remote_agents"
        )
        _tmux(
            "-L", self.outer, "new-session", "-d", "-s", "o",
            "-x", str(width), "-y", str(height), entry,
        )  # fmt: skip
        assert wait(lambda: "1" in self.bar() and "help" in self.bar()), self.screen()

    def add_session(self) -> None:
        """A managed session, recorded and marked as a launch leaves one.

        Added after the console is up, so the drill can see the bar with no row to select
        first. The sessions pane rests its cursor on the row it finds.
        """
        import asyncio

        asyncio.run(record_session(self.home, self.session_id))
        self.inner("new-session", "-d", "-s", f"ra-{self.session_id}", "sleep", "600")
        for argv in pane_mark_args(
            self.session_id, ProjectId("qualification"), ProfileId("claude")
        ):
            self.inner(*argv)

    def resize(self, width: int, height: int) -> None:
        _tmux("-L", self.outer, "resize-window", "-t", "o:", "-x", str(width), "-y", str(height))

    def screen(self, *, styled: bool = False) -> str:
        flags = ("-e",) if styled else ()
        return _tmux("-L", self.outer, "capture-pane", "-p", *flags, "-t", "o:")

    def bar(self, *, styled: bool = False) -> str:
        return self.screen(styled=styled).rstrip("\n").split("\n")[-1]

    def press(self, key: str) -> None:
        """A key typed into the owner's terminal, so the console's root bindings see it."""
        _tmux("-L", self.outer, "send-keys", "-t", "o:", key)
        time.sleep(0.8)

    def focus(self, slot: str) -> None:
        panes = self.inner(
            "list-panes", "-t", f"{CONSOLE_SESSION_NAME}:", "-F",
            "#{pane_id} #{@remote_agents_console_slot}",
        )  # fmt: skip
        pane = next(line.split()[0] for line in panes.splitlines() if line.endswith(f" {slot}"))
        self.inner("select-pane", "-t", pane)

    def close(self) -> None:
        for socket, env in ((self.outer, None), ("remote-agents", self.env)):
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"], capture_output=True, check=False, env=env
            )


@contextmanager
def drill_console(
    width: int = 200, height: int = 50, *, prepare: Callable[[Path], None] | None = None
) -> Iterator[Drill]:
    if shutil.which("tmux") is None:
        pytest.skip("BLOCKED: tmux is not installed")
    owner_before = owner_console()
    # Short on purpose: a unix socket path is limited to ~100 bytes.
    root = Path(tempfile.mkdtemp(prefix="ra-bar-", dir="/tmp"))
    home = fabricated_home(root)
    if prepare is not None:
        prepare(home)
    running = Drill(root, home, SessionId.new())
    try:
        running.start(width, height)
        yield running
    finally:
        running.close()
        shutil.rmtree(root, ignore_errors=True)
        assert owner_console() == owner_before, "the drill changed the owner's real console"
