"""Rollover notices on the bot (DEC-031, DEC-115): a rollover that failed is told once, naming
the session, the cause and that the predecessor was kept; one that completed is told nothing --
the row's redraw is its news. The real pass, over a real SQLite store, tells the real bot
boundary, whose sends land in a fake chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backends import backend_for
from fake_telegram import OWNER_CHAT_ID, OWNER_USER_ID, FakeChat

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.commands import GracefulStopCommand, LaunchCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.rollover import RolloverPass
from remote_agents.application.rollover_book import RolloverBook
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
from remote_agents.ports.terminal import (
    GRACEFUL_TIMEOUT,
    PromptDelivery,
    PromptOutcome,
    PromptReason,
    TerminalObservation,
)

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
PROJECT = "a" * 24
CLAUDE = ProfileId("claude")
HANDOFF = "h-0123456789abcdef0123"
ROOT = Path("/work/remote-agents")


def _record(session_id: SessionId, sequence: int, state=SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId(PROJECT),
        CLAUDE,
        SessionDisplayIdentity(PROJECT, "claude", "regular", sequence, None),
        state,
        NOW,
    )


@dataclass
class Host:
    """The fakes behind the pass: a session list, a terminal, a handoff directory."""

    predecessor: SessionId
    sessions: dict
    files: list
    delivery: PromptDelivery = PromptDelivery(PromptOutcome.SENT)
    stop_detail: str = ""

    async def list_sessions(self):
        return tuple(self.sessions.values())

    async def launch(self, command: LaunchCommand):
        record = _record(SessionId.new(), 2)
        self.sessions[record.session_id] = record
        return type("Launched", (), {"record": record})()

    async def send(self, session_id: SessionId, text: str) -> PromptDelivery:
        if self.delivery.outcome is PromptOutcome.SENT:
            self.files.append(
                HandoffEnvelope(HandoffEvent.ACCEPTED, HANDOFF, str(session_id), NOW, "/p", None)
            )
        return self.delivery

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        if self.stop_detail:
            return TerminalObservation(command.session_id, True, False, self.stop_detail)
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


def _rig(tmp_path: Path, **host_fields):
    store = SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3"))
    predecessor = SessionId.new()
    host = Host(predecessor, {predecessor: _record(predecessor, 1)}, [], **host_fields)
    host.files.append(
        HandoffEnvelope(HandoffEvent.READY, HANDOFF, str(predecessor), NOW, "/p", None)
    )
    chat = FakeChat()
    boundary = build_private_bot(
        OWNER_USER_ID,
        OWNER_CHAT_ID,
        backend=backend_for(
            catalogue=(CatalogProject(PROJECT, "remote-agents", "infra", "Registered"),),
            rollovers=RolloverBook(store, rollable=frozenset({CLAUDE})),
        ),
    )
    assert boundary.rollover_notifier is not None
    boundary.rollover_notifier.attach(chat.bot)

    async def on() -> bool:
        return True

    async def idle(session_id) -> bool:
        return True

    rollover_pass = RolloverPass(
        store,
        _Envelopes(host),
        rollable=frozenset({CLAUDE}),
        sessions=host.list_sessions,
        handoff_root=lambda project: ROOT,
        enabled=on,
        idle=idle,
        launch=host.launch,
        send=host.send,
        graceful_stop=host.graceful_stop,
        notify=boundary.rollover_notifier.notify,
        now=lambda: NOW,
    )
    return rollover_pass, store, chat, host, boundary.rollover_notifier


async def test_a_completed_rollover_sends_no_message(tmp_path: Path) -> None:
    rollover_pass, store, chat, host, notifier = _rig(tmp_path)

    for _ in range(3):
        await rollover_pass.run_once()

    (row,) = store._select("1 = 1", ())
    assert row.state is RolloverState.COMPLETED
    assert chat.bot_messages == []


async def test_a_failed_rollover_sends_exactly_one_message_across_passes(tmp_path: Path) -> None:
    rollover_pass, store, chat, host, notifier = _rig(
        tmp_path, delivery=PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)
    )

    for _ in range(2):
        await rollover_pass.run_once()
    # The finished handoff's `ready`, written again: the end is found again, never told again.
    host.files.append(
        HandoffEnvelope(HandoffEvent.READY, HANDOFF, str(host.predecessor), NOW, "/p", None)
    )
    for _ in range(2):
        await rollover_pass.run_once()

    (row,) = store._select("1 = 1", ())
    assert row.state is RolloverState.FAILED
    (message,) = chat.bot_messages
    assert "remote-agents" in message.text and "#1" in message.text
    assert "predecessor preserved" in message.text
    assert "not typed" in message.text
    assert host.sessions[host.predecessor].state is SessionState.RUNNING


async def test_a_stop_that_failed_sends_exactly_one_message_naming_force_stop(
    tmp_path: Path,
) -> None:
    rollover_pass, store, chat, host, notifier = _rig(tmp_path, stop_detail=GRACEFUL_TIMEOUT)

    for _ in range(3):
        await rollover_pass.run_once()

    (row,) = store._select("1 = 1", ())
    assert row.state is RolloverState.STOP_FAILED
    (message,) = chat.bot_messages
    assert "predecessor preserved" in message.text and "force stop" in message.text


async def test_a_refused_notice_is_held_and_sent_by_the_next_retry(tmp_path: Path) -> None:
    """The service loop retries held notices each tick (`pass_once`), as it does schedules'."""
    rollover_pass, store, chat, host, notifier = _rig(
        tmp_path, delivery=PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)
    )
    chat.bot.send_error = RuntimeError("chat unavailable")
    await rollover_pass.run_once()
    assert chat.bot_messages == []

    chat.bot.send_error = None
    await notifier.pass_once()
    await notifier.pass_once()

    assert len(chat.bot_messages) == 1
