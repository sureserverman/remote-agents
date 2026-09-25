"""Live drill: the real console wears the function-key bar, and the real panes feed it (DEC-105).

Everything else about the bar is proven piecewise: the format on a scratch client
(`tests/unit/adapters/tmux/test_codec_status_bar.py`), each publisher headless. What only a
drill shows is the whole: `remote-agents` entered from a bare shell, the four real pane
processes publishing through the real gateway, and the owner's terminal showing the result.

**How a real pane gets to publish here, when DEC-042 says it may not on a test socket.**
`hosting_mode` accepts only a socket *named* `remote-agents`, and the composition root
hardcodes that name. So the console here **is** named `remote-agents` -- in a private
`TMUX_TMPDIR`. tmux resolves `-L remote-agents` under `$TMUX_TMPDIR`, the server hands its
environment to every pane, and so every tmux call the panes make lands on this private
server. The hosting rule is not widened; the socket directory is isolated.

That is the exact shape of the incident DEC-042 records -- a test console driving the
owner's real one -- so it is **guarded rather than trusted**: the owner's real console
(default `TMUX_TMPDIR`) is read before and after, and the drill fails if its panes or its
bar options changed. Not opt-in, for `test_weekly_pace`'s reason: it drives its own servers
against a fabricated HOME and is a stage gate's command. It skips only without tmux.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.codec import CONSOLE_SESSION_NAME, pane_mark_args
from remote_agents.adapters.tui.keys import status_bar_keys
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_PYTHON = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"
_SGR = re.compile(r"\x1b\[([0-9;]*)m")

_REGISTRY = """version: 1
projects:
  - path: {project}
    name: qualification
    area: infra
    enabled: true
    added: 2026-09-25
"""

#: The design's two key rows (handoff README § 2), text only.
_FULL_KEYS = (
    "1 help  2 settings  3 inspect  4 detail  5 refresh  6 rename  7 add project  8 stop  "
    "9 force stop  10 close console  11 terminal  12 projects"
)
_COMPACT_KEYS = (
    "1help 2setup 3view 4detail 5refresh 6rename 7addproj 8stop 9kill 10close 12projects"
)

#: What the drill may not change on the owner's own console.
_OWNER_OPTIONS = re.compile(r"^(status|@remote_agents_(session_selected|typing|remote_control))")


def _tmux(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=check, env=env
    ).stdout


def _owner_console() -> tuple[str, ...]:
    """The owner's real console's panes and bar options, read from the default socket dir."""
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX_TMPDIR", "TMUX"}}
    panes = _tmux(
        "-L", "remote-agents", "list-panes", "-t", f"{CONSOLE_SESSION_NAME}:", "-F", "#{pane_id}",
        env=env, check=False,
    )  # fmt: skip
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


async def _record_session(home: Path, session_id: SessionId) -> None:
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


def _wait(check: Callable[[], object], bound: float = 40.0, step: float = 0.25):
    deadline = time.monotonic() + bound
    while True:
        found = check()
        if found or time.monotonic() > deadline:
            return found
        time.sleep(step)


class _Drill:
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
        assert _wait(lambda: "1" in self.bar() and "help" in self.bar()), self.screen()

    def add_session(self) -> None:
        """A managed session, recorded and marked as a launch leaves one.

        Added after the console is up, so the drill can see the bar with no row to select
        first. The sessions pane rests its cursor on the row it finds.
        """
        import asyncio

        asyncio.run(_record_session(self.home, self.session_id))
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


@pytest.fixture(scope="module")
def drill() -> Iterator[_Drill]:
    if shutil.which("tmux") is None:
        pytest.skip("BLOCKED: tmux is not installed")
    owner_before = _owner_console()
    # Short on purpose: a unix socket path is limited to ~100 bytes.
    root = Path(tempfile.mkdtemp(prefix="ra-bar-", dir="/tmp"))
    home = _fabricated_home(root)
    running = _Drill(root, home, SessionId.new())
    try:
        running.start(200, 50)
        yield running
    finally:
        running.close()
        shutil.rmtree(root, ignore_errors=True)
        assert _owner_console() == owner_before, "the drill changed the owner's real console"


def _fg_at(styled_row: str, text: str) -> str:
    """The SGR foreground in force where *text* begins in a styled captured row."""
    plain, colours, current = [], [], ""
    index = 0
    while index < len(styled_row):
        match = _SGR.match(styled_row, index)
        if match:
            params = match.group(1).split(";")
            for i, param in enumerate(params):
                if param in {"38"}:
                    current = ";".join(params[i : i + (5 if params[i + 1] == "2" else 3)])
                elif param in {"0", "39", ""}:
                    current = ""
            index = match.end()
            continue
        plain.append(styled_row[index])
        colours.append(current)
        index += 1
    return colours["".join(plain).index(text)]


def _remote_control_words(screen: str) -> dict[str, str]:
    """The Remote Control lines' state words as a pane drew them (Settings, since the facelift)."""
    words = {}
    for line in screen.splitlines():
        # Stops at whichever edge the pane draws: a round border's `│` or a tall one's `▎`.
        found = re.search(r"(Claude|Codex) Remote Control · (.+?)\s*(?:[│▎▕]|$)", line)
        if found:
            words[found.group(1).lower()] = found.group(2).strip()
    return words


# --- the drill ---------------------------------------------------------------------------------


def test_the_full_bar_at_200_reads_the_design_line_and_every_right_end_fits(drill) -> None:
    """The keys as the design draws them, and a right end that is whole, never clipped.

    Waited on until the limits pane has published, because the right end before that is only
    the session name, which fits anything and proves nothing.
    """
    drill.resize(200, 50)
    row = _wait(lambda: _published(drill.bar()))

    assert row, drill.bar()
    assert row.startswith(_FULL_KEYS + " "), row
    assert len(row) == 200
    right = row[len(_FULL_KEYS) :].strip()
    assert right.endswith(CONSOLE_SESSION_NAME), row
    # Whatever the words, the right end is whole: never a fragment cut off at its left.
    assert right.startswith(("Remote Control  ", "RC ")), row


def _published(row: str) -> str | None:
    """*row*, once the limits pane's Remote Control reading is on it (words or marks)."""
    return row if ("Remote Control  claude" in row or " RC " in row) else None


def test_the_bar_s_remote_control_words_equal_the_settings_lines(drill) -> None:
    """The bar and Settings agree, and the limits pane no longer draws either line (R10).

    Settings reads the two facts on its own path and keeps their full wording under console
    hosting (DEC-084/DEC-085), so it is the independent witness the limits pane was until the
    console facelift moved these lines off it.
    """
    drill.resize(240, 50)
    assert _wait(lambda: "Remote Control  claude" in drill.bar() or None), drill.bar()
    assert not _remote_control_words(drill.screen()), (
        f"the limits pane still draws a Remote Control line: {drill.screen()}"
    )

    drill.focus("feed")
    drill.press("F2")

    def settings_open() -> str | None:
        screen = drill.screen()
        return screen if len(_remote_control_words(screen)) == 2 else None

    screen = _wait(settings_open)
    assert screen, drill.screen()
    words = _remote_control_words(screen)
    bar = drill.bar()
    drill.press("Escape")

    assert f"Remote Control  claude {words['claude']} · codex {words['codex']}  " in bar, (
        bar,
        words,
    )
    drill.resize(200, 50)


def test_the_compact_bar_at_100_has_no_f11(drill) -> None:
    drill.resize(100, 30)
    row = _wait(lambda: (bar := drill.bar()).startswith("1help") and bar)
    drill.resize(200, 50)

    assert row.startswith(_COMPACT_KEYS + " "), row
    assert "11" not in row
    assert row.rstrip().endswith(("RC ", "?", "●", "○")) or row.rstrip() == _COMPACT_KEYS, row


def test_session_keys_carry_the_dim_colour_with_no_row_selected(drill) -> None:
    drill.resize(200, 50)
    styled = _wait(lambda: drill.bar(styled=True) if drill.bar().startswith(_FULL_KEYS) else None)
    assert styled, drill.bar()
    dim = _fg_at(styled, "11 terminal")
    key = _fg_at(styled, "1 help")

    assert dim != key
    for entry in status_bar_keys():
        if not entry.bound:
            continue
        colour = _fg_at(styled, f"{entry.number} {entry.label}")
        assert (colour == dim) is entry.needs_selection, (entry.label, colour, dim, key)


def test_a_selected_row_lights_the_keys_and_the_rename_box_says_esc_cancels(drill) -> None:
    drill.resize(200, 50)
    drill.add_session()
    drill.focus("sessions")

    def lit_row() -> str | None:
        styled = drill.bar(styled=True)
        return styled if _fg_at(styled, "3 inspect") != _fg_at(styled, "11 terminal") else None

    lit = _wait(lit_row, bound=40)
    assert lit, drill.screen()

    drill.press("F6")
    typing = _wait(lambda: "esc cancels" in drill.bar(), bound=15)
    screen = drill.screen()
    drill.press("Escape")
    back = _wait(lambda: "esc cancels" not in drill.bar(), bound=15)
    # F6 opened the session's detail under the rename box; leave it too, so the pane is
    # back on its list for the next drill step.
    drill.press("Escape")
    listed = _wait(lambda: "Sessions ›" not in drill.screen(), bound=15)

    assert typing, screen
    assert back, drill.screen()
    assert listed, drill.screen()


def test_the_bar_survives_an_agent_in_the_left_slot_and_the_f12_round_trip(drill) -> None:
    drill.resize(200, 50)
    drill.focus("sessions")
    drill.press("Enter")
    shown = _wait(
        lambda: "sleep" in drill.inner(
            "display-message", "-p", "-t", f"{CONSOLE_SESSION_NAME}:.0", "#{pane_current_command}"
        ),
        bound=15,
    )  # fmt: skip
    assert shown, drill.screen()
    assert drill.bar().startswith(_FULL_KEYS), "the bar left with the surface"

    drill.press("F12")
    home = _wait(
        lambda: "sleep" not in drill.inner(
            "display-message", "-p", "-t", f"{CONSOLE_SESSION_NAME}:.0", "#{pane_current_command}"
        ),
        bound=15,
    )  # fmt: skip
    assert home, drill.screen()
    assert drill.bar().startswith(_FULL_KEYS)


def test_the_bar_takes_only_the_row_tmux_s_own_status_line_took_at_80x24(drill) -> None:
    """R8, measured rather than assumed: the bar costs the panes no row.

    R8 expected every pane to lose a row. It does not: tmux's `status` is `on` by default, so
    the console already gave its bottom row to tmux's own status line (the owner's console on
    2026-09-25: a 45-row window in a 46-row client). The bar replaces that line. So the
    property is that the window is exactly one row shorter than the client -- the default's
    cost -- and not two. What the limits pane holds at 80x24 is therefore unchanged by this
    plan (DEC-100's 58% cap is the dashboard's, which bare `tui` draws with no bar at all).
    """
    drill.resize(80, 24)
    heights = _wait(
        lambda: (
            drill.inner(
                "display-message", "-p", "-t", f"{CONSOLE_SESSION_NAME}:",
                "#{window_height} #{client_height}",
            ).split()
            if drill.bar().startswith("1help")
            else None
        ),
        bound=15,
    )  # fmt: skip
    default_status = drill.inner("show-options", "-gv", "status").strip()
    drill.resize(200, 50)

    assert heights, drill.screen()
    window, client = (int(value) for value in heights)
    assert default_status == "on", "tmux's own default would not have drawn a status line"
    assert window == client - 1, (window, client)
