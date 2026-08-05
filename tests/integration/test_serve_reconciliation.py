"""The service keeps durable records agreeing with observed panes while it polls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.application.reconcile import ReconciliationService
from remote_agents.bootstrap import ServiceComposition, _serve_with_reconciliation
from remote_agents.config import TelegramSecrets
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent
from remote_agents.ports.terminal import TerminalObservation

_SECRETS = TelegramSecrets("token", 7, 11)


class StubTerminal:
    """Report exactly the panes a test says the tmux server currently holds."""

    def __init__(self, observations: tuple[TerminalObservation, ...] = ()) -> None:
        self.observations = observations
        self.passes = 0

    async def managed_observations(self) -> tuple[TerminalObservation, ...]:
        self.passes += 1
        return self.observations


class FailingTerminal(StubTerminal):
    async def managed_observations(self) -> tuple[TerminalObservation, ...]:
        self.passes += 1
        raise RuntimeError("tmux command failed")


def _stuck_record(session_id: SessionId, *, age: timedelta = timedelta(hours=1)) -> SessionRecord:
    """A launch that raised after its record was saved leaves exactly this behind."""
    return SessionRecord(
        session_id,
        ProjectId("opaque-project"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-project", "claude", "regular", 1),
        SessionState.STARTING,
        datetime.now(UTC) - age,
    )


def _composition(store: SQLiteSessionStore, terminal: StubTerminal) -> ServiceComposition:
    return ServiceComposition(PrivateBotBoundary(7, 11), terminal, ReconciliationService(store))


async def test_a_record_stuck_in_starting_is_resolved_before_polling_begins(
    tmp_path: Path,
) -> None:
    """No owner action can resolve STARTING, so the service must resolve it itself."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        session_id = SessionId.new()
        await store.save(_stuck_record(session_id))
        terminal = StubTerminal()

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            assert (await store.get(session_id)).state is SessionState.FAILED

        await _serve_with_reconciliation(_SECRETS, _composition(store, terminal), poll, 3600)

        assert (await store.get(session_id)).state is SessionState.FAILED
    finally:
        connection.close()


async def test_a_starting_record_whose_pane_is_live_becomes_running(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        session_id = SessionId.new()
        await store.save(_stuck_record(session_id))
        terminal = StubTerminal(
            (
                TerminalObservation(
                    session_id,
                    live=True,
                    preserved=False,
                    project_id=ProjectId("opaque-project"),
                    profile_id=ProfileId("claude"),
                ),
            )
        )

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            return None

        await _serve_with_reconciliation(_SECRETS, _composition(store, terminal), poll, 3600)

        assert (await store.get(session_id)).state is SessionState.RUNNING
    finally:
        connection.close()


async def test_reconciliation_keeps_running_beside_the_poll(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        terminal = StubTerminal()

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            while terminal.passes < 3:
                await asyncio.sleep(0)

        await _serve_with_reconciliation(_SECRETS, _composition(store, terminal), poll, 0)

        assert terminal.passes >= 3
    finally:
        connection.close()


async def test_the_periodic_pass_stops_when_polling_returns(tmp_path: Path) -> None:
    """A cancelled reconciliation must not outlive the service or swallow its shutdown."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        terminal = StubTerminal()

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            await asyncio.sleep(0)

        await _serve_with_reconciliation(_SECRETS, _composition(store, terminal), poll, 0)
        settled = terminal.passes
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert terminal.passes == settled
    finally:
        connection.close()


async def test_a_failing_reconciliation_never_takes_the_service_down(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        terminal = FailingTerminal()
        polled = []

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            polled.append(secrets)

        await _serve_with_reconciliation(_SECRETS, _composition(store, terminal), poll, 3600)

        assert polled == [_SECRETS]
    finally:
        connection.close()


async def test_a_failing_poll_still_stops_the_periodic_pass(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        terminal = StubTerminal()

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            raise RuntimeError("polling failed")

        with pytest.raises(RuntimeError):
            await _serve_with_reconciliation(_SECRETS, _composition(store, terminal), poll, 0)

        settled = terminal.passes
        await asyncio.sleep(0)
        assert terminal.passes == settled
    finally:
        connection.close()


class _EmptyOnAbsentServer:
    """Model the adapter's real contract: an absent server is empty, a failure raises."""

    def __init__(self, *, absent: bool) -> None:
        self.absent = absent

    async def managed_observations(self) -> tuple[TerminalObservation, ...]:
        if self.absent:
            return ()
        raise RuntimeError("tmux command failed: connection refused")


async def test_a_launch_still_in_flight_is_left_to_the_call_that_started_it(
    tmp_path: Path,
) -> None:
    """A pane exists before its agent reports ready; promoting it crashes the launcher."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        session_id = SessionId.new()
        await store.save(_stuck_record(session_id, age=timedelta(seconds=1)))
        terminal = StubTerminal(
            (
                TerminalObservation(
                    session_id,
                    live=True,
                    preserved=False,
                    project_id=ProjectId("opaque-project"),
                    profile_id=ProfileId("claude"),
                ),
            )
        )

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            return None

        await _serve_with_reconciliation(_SECRETS, _composition(store, terminal), poll, 3600)

        assert (await store.get(session_id)).state is SessionState.STARTING
    finally:
        connection.close()


async def test_a_query_failure_never_ends_a_live_session_record(tmp_path: Path) -> None:
    """An empty result means every session is gone, so a failed query must not look empty."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        session_id = SessionId.new()
        await store.save(_stuck_record(session_id))
        await store.record_event(session_id, LifecycleEvent.READY)
        assert (await store.get(session_id)).state is SessionState.RUNNING

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            return None

        await _serve_with_reconciliation(
            _SECRETS,
            _composition(store, _EmptyOnAbsentServer(absent=False)),
            poll,
            3600,
        )

        assert (await store.get(session_id)).state is SessionState.RUNNING
    finally:
        connection.close()


async def test_an_absent_server_does_end_a_record_it_no_longer_holds(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        store = SQLiteSessionStore(connection)
        session_id = SessionId.new()
        await store.save(_stuck_record(session_id))
        await store.record_event(session_id, LifecycleEvent.READY)

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            return None

        await _serve_with_reconciliation(
            _SECRETS, _composition(store, _EmptyOnAbsentServer(absent=True)), poll, 3600
        )

        assert (await store.get(session_id)).state is SessionState.ENDED
    finally:
        connection.close()
