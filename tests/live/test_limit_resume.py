"""Live drill: a Codex limit screen is seen, its lift observed, and the nudge typed exactly once.

Limit-lifecycle sub-plan 2 Task 3.5. Everything the service decides is exercised for real here:
the limit-screen watch capturing a real tmux pane, the classifier naming the window from the
limits reading, the stop recorded in a real SQLite store and delivered through the real
notifier, the lift decided by the real lift pass, and the nudge typed through the real
`TmuxTerminal.send_prompt` with its guards -- all driven by the service's own periodic loops.

**What is not real is the agent.** The pane runs a stub under the Codex profile that replays the
measured Codex screens (`tests/fixtures/panes/codex/idle_0158.txt`, `composed_0158.txt`, and the
limit sentence from `codex/limit_screen.py`): an idle composer, the limit sentence when the test
says so, and whatever is typed drawn into its composer. So it spends no quota. The limits reader
is injected at the composition seam, reading the 5-hour window full until a reset a few seconds
after the stop. The lift then waits out the real grace minute, so the drill takes about 80 s.

**Not opt-in**, for `test_prompt_relay.py`'s reason: it is a gate's command. It skips only when
tmux is missing. Boundaries: a `remote-agents-test-*` socket that is killed, and a scratch
directory for everything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from remote_agents.adapters.agents.registry import (
    profile_composers,
    profile_limit_screens,
    provider_descriptors,
)
from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.limit_stop_store import SQLiteLimitStopStore
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.notifications import ActivityNotifier
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TerminalWaits, TmuxTerminal
from remote_agents.application.limit_lifts import LimitLiftWatcher
from remote_agents.application.limit_resume import NUDGE, RESUMED, LimitResume
from remote_agents.application.limit_stops import LimitScreenWatcher, LimitStopClassifier
from remote_agents.composition.service import (
    ServiceComposition,
    _watch_activity_periodically,
    _watch_limit_stops_periodically,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

_PANES = Path(__file__).resolve().parents[1] / "fixtures" / "panes" / "codex"
_ROWS = 40
_PASS_SECONDS = 2.0

#: The stub: raw mode, bottom-aligned screens replayed from the measured fixtures. Arguments: the
#: trigger file (present = draw the limit sentence), the log of submitted drafts, the model line
#: and the hint line, both taken verbatim from `idle_0158.txt`.
_STUB = r"""
import os, select, sys, termios, tty
from pathlib import Path

trigger, log, model, hint = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
rows = int(sys.argv[5])
draft = ""
limited = False


def draw():
    body = ["• Working on the task."]
    if limited:
        body += ["", "■ You’ve hit your usage limit. Upgrade to Pro for more usage."]
    if draft:
        body += ["", "› " + draft, "", model, ""]
    else:
        body += ["", "› Ask Codex to do anything", "", model, hint]
    lines = [""] * (rows - len(body)) + body
    sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(lines))
    sys.stdout.flush()


tty.setraw(sys.stdin.fileno())
draw()
while True:
    ready, _, _ = select.select([sys.stdin], [], [], 0.2)
    if not limited and trigger.exists():
        limited = True
        draw()
    if not ready:
        continue
    for character in os.read(sys.stdin.fileno(), 1024).decode("utf-8", "replace"):
        if character == "\r":
            if draft:
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(draft + "\n")
                draft = ""
        elif character.isprintable():
            draft += character
    draw()
"""


class _View:
    """The `LiveView` surface the notifier uses, recording what the chat was made to show."""

    chat_id = 11

    def __init__(self) -> None:
        self.written: list[str] = []
        self.deleted: list[int] = []
        self._next = 900

    async def send_apart(self, _bot, arguments) -> int:
        self.written.append(str(arguments["text"]))
        self._next += 1
        return self._next

    async def amend_apart(self, _bot, _message_id, arguments) -> bool:
        self.written.append(str(arguments["text"]))
        return True

    async def discard(self, _bot, message_id) -> bool:
        self.deleted.append(message_id)
        return True


class _Bot:
    async def edit_message_reply_markup(self, **_kwargs) -> None:
        return None


def _tmux(socket: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments], capture_output=True, text=True, timeout=30, check=False
    )


def _screen_lines() -> tuple[str, str]:
    idle = (_PANES / "idle_0158.txt").read_text(encoding="utf-8").rstrip("\n").split("\n")
    return idle[-2], idle[-1]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_a_codex_limit_is_seen_lifted_and_resumed_exactly_once(tmp_path: Path) -> None:
    asyncio.run(_drill(tmp_path))


async def _drill(tmp_path: Path) -> None:
    socket = f"remote-agents-test-{uuid4().hex[:12]}"
    trigger, log = tmp_path / "limited", tmp_path / "submitted.log"
    stub = tmp_path / "stub_codex.py"
    stub.write_text(_STUB, encoding="utf-8")
    model, hint = _screen_lines()
    session_id = SessionId.new()
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    store = SQLiteSessionStore(connection)
    await store.save(
        SessionRecord(
            session_id,
            ProjectId("qualification"),
            ProfileId("codex"),
            SessionDisplayIdentity("qualification", "codex", "drill", 1),
            SessionState.RUNNING,
            datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    command = [sys.executable, str(stub), str(trigger), str(log), model, hint, str(_ROWS)]
    started = _tmux(
        socket, "new-session", "-d", "-s", f"ra-{session_id}", "-x", "160", "-y", str(_ROWS),
        "-c", str(tmp_path), *command,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    loops: list[asyncio.Task] = []
    try:
        pane = _tmux(socket, "list-panes", "-t", f"=ra-{session_id}:", "-F", "#{pane_id}")
        pane_id = pane.stdout.strip()
        for option, value in (
            ("@remote_agents_schema", "2"),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", "qualification"),
            ("@remote_agents_profile", "codex"),
        ):
            _tmux(socket, "set-option", "-p", "-t", pane_id, option, value)

        terminal = TmuxTerminal(
            TmuxGateway(socket, AsyncTmuxRunner(), key_lock_directory=tmp_path / "locks"),
            {},
            {},
            startup_timeout=1.0,
            composers=profile_composers(),
            waits=TerminalWaits(prompt_settle=0.5, prompt_bound=20.0),
        )
        reset: list[datetime] = []

        async def limits() -> tuple[AgentLimits, ...]:
            if not reset:
                return ()
            window = UsageWindow("5h", 100, reset[0])
            return (AgentLimits("codex", (window,), observed_at=datetime.now(UTC)),)

        view = _View()

        async def display(_session: str) -> str:
            return "qualification · codex · drill"

        notifier = ActivityNotifier(
            view=view, callbacks=CallbackStateStore(), owner_user_id=7, display=display
        )
        notifier.attach(_Bot())

        async def enabled() -> bool:
            return True

        screens = profile_limit_screens(provider_descriptors())
        composition = ServiceComposition(
            SimpleNamespace(notifier=notifier),  # type: ignore[arg-type]
            terminal,
            None,  # type: ignore[arg-type]
            None,
            None,
            SQLiteActivityStore(connection),
            limit_classifier=LimitStopClassifier(store, limits, screens),
            limit_screen_watcher=LimitScreenWatcher(store, terminal.capture, screens),
            limit_lift_watcher=LimitLiftWatcher(
                store,
                SQLiteLimitStopStore(connection),
                limits,
                lambda stop: notifier.retire_line(
                    stop.session_id, ActivityKind.LIMIT_REACHED, observed_at=stop.stopped_at
                ),
                resume=LimitResume(
                    send=terminal.send_prompt,
                    enabled=enabled,
                    settled=lambda stop: notifier.line_settled(
                        stop.session_id, ActivityKind.LIMIT_REACHED, observed_at=stop.stopped_at
                    ),
                    amend=lambda stop, reason: notifier.retire_line(
                        stop.session_id,
                        ActivityKind.LIMIT_REACHED,
                        observed_at=stop.stopped_at,
                        note=reason,
                    ),
                ),
            ),
        )
        loops = [
            asyncio.create_task(_watch_activity_periodically(composition, _PASS_SECONDS)),
            asyncio.create_task(_watch_limit_stops_periodically(composition, _PASS_SECONDS)),
        ]
        # A few passes over the idle screen first: the watch's first sight of a pane only seeds
        # its count, so a stop has to be drawn after that to be news.
        await asyncio.sleep(3 * _PASS_SECONDS)
        reset.append(datetime.now(UTC) + timedelta(seconds=5))
        trigger.touch()

        await _until(lambda: _stops(connection, session_id) == 1, 30, "the stop was never seen")
        await _until(lambda: any("5-hour" in text for text in view.written), 15, "not delivered")
        await _until(lambda: _outcomes(connection) == [RESUMED], 120, "the lift never resumed it")
        await asyncio.sleep(4 * _PASS_SECONDS)

        submitted = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        assert submitted == [NUDGE], submitted
        assert _stops(connection, session_id) == 1, "the nudge made no second stop"
        assert view.deleted, "the limit line, the message's only line, was taken out"
        assert notifier._recall(str(session_id)) is None
    finally:
        for task in loops:
            task.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        _tmux(socket, "kill-server")
        connection.close()


def _stops(connection, session_id: SessionId) -> int:
    (count,) = connection.execute(
        "SELECT COUNT(*) FROM agent_activity WHERE session_id = ? AND kind = ?",
        (str(session_id), ActivityKind.LIMIT_REACHED.value),
    ).fetchone()
    return count


def _outcomes(connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT outcome FROM limit_stop_outcomes")]


async def _until(condition, seconds: float, why: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if condition():
                return
        await asyncio.sleep(0.5)
    pytest.fail(why)
