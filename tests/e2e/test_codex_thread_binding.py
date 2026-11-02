"""A fresh Codex successor binds by the real line, adopts, and its predecessor stops.

Synthetic terminal, real everything else: the binder, the store, the rollover pass, and the printed
`!` line run through a shell as the pane would run it, so the verb and `session-id` are the shipped
entry points. Native acceptance is `tests/live/test_codex_thread_binding_live.py`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from remote_agents.adapters.agents.hook_identity import RosterEntry, roster_path, write_roster
from remote_agents.adapters.agents.thread_binding import FileThreadBindings
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.tmux.profiles import bind_thread_line
from remote_agents.application.commands import GracefulStopCommand, LaunchCommand
from remote_agents.application.rollover import BINDING_FAILED, RolloverPass
from remote_agents.application.thread_binding import ThreadBinder
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.handoff_envelopes import HandoffEnvelope, HandoffEvent
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, TerminalObservation

_CODEX = ProfileId("codex")
_PROJECT = ProjectId("p")
_ROOT = Path("/p")
_HANDOFF = "h-0123456789abcdef0123"
_THREAD = "01a193e7-e8c8-7272-9c5f-6ee9e7749464"
_TEMPLATE = "adopt {handoff_id}"
_NOW = datetime(2026, 10, 31, tzinfo=UTC)


def _record(session_id: SessionId, sequence: int, state=SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        session_id,
        _PROJECT,
        _CODEX,
        SessionDisplayIdentity("p", "codex", "regular", sequence),
        state,
        _NOW,
    )


def _shell(line: str, spool: Path) -> tuple[int, str, str]:
    """Run what the pane would, with the variable Codex sets and nothing the service's has."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("REMOTE_")}
    environment["CODEX_THREAD_ID"] = _THREAD
    done = subprocess.run(
        line.removeprefix("!"), shell=True, env=environment, capture_output=True, text=True
    )
    return done.returncode, done.stdout, done.stderr


@dataclass
class Host:
    spool: Path
    predecessor: SessionId
    sessions: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    order: list = field(default_factory=list)
    shell_runs: bool = True
    clock: list = field(default_factory=lambda: [_NOW])
    bound_answers: list = field(default_factory=list)

    def roster(self) -> None:
        write_roster(
            roster_path(self.spool, "codex"),
            [RosterEntry(str(key), "/w", _NOW) for key in self.sessions],
        )

    async def list_sessions(self):
        return tuple(self.sessions.values())

    async def launch(self, command: LaunchCommand):
        record = _record(SessionId.new(), 2)
        FileThreadBindings(self.spool).request(str(record.session_id))
        self.sessions[record.session_id] = record
        self.roster()
        return type("Launched", (), {"record": record})()

    async def send_line(self, session_id: SessionId, line: str) -> PromptDelivery:
        self.order.append("line")
        if self.shell_runs:
            assert _shell(line, self.spool)[0] == 0
        return PromptDelivery(PromptOutcome.SENT)

    async def send(self, session_id: SessionId, text: str) -> PromptDelivery:
        self.order.append("template")
        # The adopting skill asks the real verb which session it is.
        supervisor = shlex.join(
            [
                sys.executable,
                "-I",
                "-m",
                "remote_agents",
                "session-id",
                "--activity-dir",
                str(self.spool),
            ]
        )
        code, printed, _ = _shell(supervisor, self.spool)
        self.bound_answers.append((code, printed.strip()))
        if code == 0:
            self.files.append(
                HandoffEnvelope(HandoffEvent.ACCEPTED, _HANDOFF, printed.strip(), _NOW, "/p", None)
            )
        return PromptDelivery(PromptOutcome.SENT)

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        self.order.append("stop")
        self.sessions[command.session_id] = _record(command.session_id, 1, SessionState.ENDED)
        return TerminalObservation(command.session_id, False, True)


class _Envelopes:
    def __init__(self, host: Host) -> None:
        self._host = host

    def events(self, project_dir):
        return tuple(self._host.files)

    def write_request(self, project_dir, managed_session_id):
        return True

    def requested(self, project_dir):
        return None

    def discard(self, project_dir, handoff_id):
        self._host.files[:] = [e for e in self._host.files if e.handoff_id != handoff_id]

    def clear_request(self, project_dir, managed_session_id):
        return None


def _rig(tmp_path: Path, **fields) -> tuple[RolloverPass, SQLiteRolloverStore, Host]:
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    store = SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3"))
    predecessor = SessionId.new()
    host = Host(spool, predecessor, **fields)
    host.sessions[predecessor] = _record(predecessor, 1)
    host.roster()
    host.files.append(
        HandoffEnvelope(HandoffEvent.READY, _HANDOFF, str(predecessor), _NOW, "/p", None)
    )

    async def on() -> bool:
        return True

    async def idle(session_id) -> bool:
        return True

    async def sleep(seconds) -> None:
        host.clock[0] += timedelta(seconds=seconds)

    binder = ThreadBinder(
        FileThreadBindings(spool),
        sessions=host.list_sessions,
        hooks={"codex": "codex"},
        send_line=host.send_line,
        line_for=lambda session, nonce: bind_thread_line(spool, session, nonce),
        now=lambda: host.clock[0],
        sleep=sleep,
    )
    rollover_pass = RolloverPass(
        store,
        _Envelopes(host),
        adopt_templates={_CODEX: _TEMPLATE},
        sessions=host.list_sessions,
        handoff_root=lambda project: _ROOT,
        enabled=on,
        idle=idle,
        launch=host.launch,
        send=host.send,
        bound=binder.wait_bound,
        graceful_stop=host.graceful_stop,
        now=lambda: _NOW,
    )
    return rollover_pass, store, host


async def test_thread_binding_a_fresh_successor_binds_adopts_and_the_predecessor_stops(
    tmp_path: Path,
) -> None:
    rollover_pass, store, host = _rig(tmp_path)
    for _ in range(4):
        await rollover_pass.run_once()
    (row,) = store._select("1 = 1", ())
    assert row.state is RolloverState.COMPLETED, row
    assert host.order == ["line", "template", "stop"]
    assert host.bound_answers == [(0, str(row.successor_session_id))]
    assert host.sessions[host.predecessor].state is SessionState.ENDED


async def test_thread_binding_not_seen_keeps_the_predecessor(tmp_path: Path) -> None:
    rollover_pass, store, host = _rig(tmp_path, shell_runs=False)
    for _ in range(3):
        await rollover_pass.run_once()
    (row,) = store._select("1 = 1", ())
    assert row.state is RolloverState.FAILED
    assert row.failure_code == BINDING_FAILED
    assert host.order == ["line"], "no template, and no stop"
    assert host.sessions[host.predecessor].state is SessionState.RUNNING
