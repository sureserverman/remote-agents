"""Restart, outage, duplicate-delivery, and shutdown resilience at real local boundaries."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_terminal_launch import STARTUP_BUDGET, make_terminal

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.authorization import (
    AuthorizationGate,
    AuthorizationUpdate,
    ContentFreeDenialLog,
)
from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.application.commands import GracefulStopCommand, InspectQuery, LaunchCommand
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.runtime import RuntimeCoordinator
from remote_agents.application.services import SessionService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import TerminalObservation


class RecordedUpdate:
    def __init__(
        self,
        token: str,
        *,
        sender_id: int | None = None,
        chat_id: int | None = None,
        chat_type: str | None = None,
    ) -> None:
        self.token = token
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.chat_type = chat_type


class FakeTelegramTransport:
    def __init__(self, batches, *, failures: int = 0) -> None:
        self._batches = list(batches)
        self._failures = failures

    async def get_updates(self):
        if self._failures:
            self._failures -= 1
            raise RuntimeError("synthetic_poll_failure")
        return self._batches.pop(0) if self._batches else ()


class PollingAdapter:
    def __init__(self, transport, authorization, handle, *, retries: int, wait) -> None:
        self._transport = transport
        self._authorization = authorization
        self._handle = handle
        self._retries = retries
        self._wait = wait

    async def poll_once(self) -> None:
        for attempt in range(self._retries + 1):
            try:
                updates = await self._transport.get_updates()
                break
            except RuntimeError:
                if attempt == self._retries:
                    raise
                await self._wait(0)
        for update in updates:
            if self._authorization.dispatch(
                AuthorizationUpdate(
                    sender_id=update.sender_id,
                    chat_id=update.chat_id,
                    chat_type=update.chat_type,
                    kind="callback",
                ),
                lambda: None,
            ):
                self._handle(update.token)


def _record(session_id: SessionId, state: SessionState, sequence: int) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("fake"),
        SessionDisplayIdentity("opaque-editor", "fake", "regular", sequence),
        state,
        datetime.now(UTC),
    )


async def test_restart_reconciles_launch_running_stop_requested_and_preserved_states(
    tmp_path: Path,
) -> None:
    """A fresh reconciler recovers every persisted state from the isolated tmux evidence."""
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    database_path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(open_database(database_path))
    starting, running, stopping, preserved = (SessionId.new() for _ in range(4))
    try:
        for sequence, (session_id, state) in enumerate(
            (
                (starting, SessionState.STARTING),
                (running, SessionState.RUNNING),
                (stopping, SessionState.STOP_REQUESTED),
                (preserved, SessionState.PRESERVED),
            ),
            start=1,
        ):
            assert (
                await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))
            ).live
            if state is SessionState.PRESERVED:
                assert (await terminal.graceful_stop(session_id, ProfileId("fake"))).preserved
            await store.save(_record(session_id, state, sequence))

        restarted_store = SQLiteSessionStore(open_database(database_path))
        results = await ReconciliationService(restarted_store, settle_after=timedelta(0)).reconcile(
            await terminal.managed_observations()
        )

        assert {item.session_id: item.state for item in results} == {
            starting: SessionState.RUNNING,
            running: SessionState.RUNNING,
            stopping: SessionState.RUNNING,
            preserved: SessionState.PRESERVED,
        }
        assert (await restarted_store.get(starting)).state is SessionState.RUNNING
        assert (await restarted_store.get(stopping)).state is SessionState.RUNNING
        assert (await restarted_store.get(preserved)).state is SessionState.PRESERVED
    finally:
        for session_id in (starting, running, stopping, preserved):
            try:
                await gateway.mutate("kill-session", f"ra-{session_id}")
            except RuntimeError:
                pass


async def test_restart_can_gracefully_stop_a_running_managed_session(tmp_path: Path) -> None:
    """Restart recovery must retain profile-owned stop behavior, not only inspection."""
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    database_path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(open_database(database_path))
    service = SessionService(store, terminal)
    command = LaunchCommand(ProjectId("opaque-editor"), ProfileId("fake"), "restart-stop")
    record = None
    try:
        record = await service.launch(command)
        assert record.state is SessionState.RUNNING

        restarted = TmuxTerminal(
            gateway,
            {ProjectId("opaque-editor"): tmp_path},
            {
                ProfileId("fake"): LaunchProfile(
                    sys.executable,
                    (sys.executable, str(tmp_path / "fake_agent.py"), "ready"),
                    {"PATH": os.environ["PATH"]},
                    "READY",
                )
            },
            startup_timeout=STARTUP_BUDGET,
        )
        stopped = await SessionService(
            SQLiteSessionStore(open_database(database_path)), restarted
        ).graceful_stop(GracefulStopCommand(record.session_id, record.profile_id))

        assert stopped.preserved
        assert (await store.get(record.session_id)).state is SessionState.ENDED
    finally:
        if record is not None:
            try:
                await gateway.mutate("kill-session", f"ra-{record.session_id}")
            except RuntimeError:
                pass


async def test_polling_retries_a_transient_outage_without_authorizing_foreign_updates() -> None:
    handled: list[str] = []
    waits: list[float] = []
    transport = FakeTelegramTransport(
        (
            (
                RecordedUpdate("owner", sender_id=7, chat_id=11, chat_type="private"),
                RecordedUpdate("foreign", sender_id=8, chat_id=11, chat_type="private"),
            ),
        ),
        failures=1,
    )

    async def wait(delay: float) -> None:
        waits.append(delay)

    polling = PollingAdapter(
        transport,
        AuthorizationGate(7, 11, ContentFreeDenialLog()),
        handled.append,
        retries=1,
        wait=wait,
    )

    await polling.poll_once()

    assert waits == [0]
    assert handled == ["owner"]


async def test_duplicate_launch_delivery_creates_one_real_session(tmp_path: Path) -> None:
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    service = SessionService(store, terminal)
    command = LaunchCommand(ProjectId("opaque-editor"), ProfileId("fake"), "duplicate-update")
    try:
        first, second = await asyncio.gather(
            service.launch(command), service.launch(command), return_exceptions=True
        )

        records = await service.list_sessions()
        assert sum(not isinstance(item, Exception) for item in (first, second)) == 1
        assert len(records) == 1
        assert records[0].state is SessionState.RUNNING
        assert len(await terminal.managed_observations()) == 1
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.mutate("kill-session", f"ra-{record.session_id}")
            except RuntimeError:
                pass


async def test_concurrent_inspect_and_graceful_stop_preserve_one_session(tmp_path: Path) -> None:
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    service = SessionService(store, terminal)
    try:
        record = await service.launch(
            LaunchCommand(ProjectId("opaque-editor"), ProfileId("fake"), "inspect-stop")
        )
        inspected, stopped = await asyncio.gather(
            service.inspect(InspectQuery(record.session_id)),
            service.graceful_stop(GracefulStopCommand(record.session_id, record.profile_id)),
        )

        assert inspected is None or inspected.session_id == record.session_id
        assert stopped.preserved
        assert (await store.get(record.session_id)).state is SessionState.ENDED
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.mutate("kill-session", f"ra-{record.session_id}")
            except RuntimeError:
                pass


async def test_reconciler_failure_stops_polling_drains_and_propagates() -> None:
    polling = BlockingPolling()
    drainer = RecordingDrainer()
    runtime = RuntimeCoordinator(
        polling=polling,
        reconciler=FailingReconciler(),
        terminal=NoopTerminal(),
        drainer=drainer,
        reconcile_interval=0,
    )

    with pytest.raises(RuntimeError, match="synthetic reconciliation failure"):
        await runtime.run()

    assert polling.stopped.is_set()
    assert drainer.drained


async def test_shutdown_waits_for_a_transaction_started_before_sigterm() -> None:
    locks = SessionLocks()
    terminal = BlockingTerminal()
    service = SessionService(InMemoryStore(), terminal, locks=locks)
    polling = BlockingPolling()
    runtime = RuntimeCoordinator(
        polling=polling,
        reconciler=NoopReconciler(),
        terminal=NoopTerminal(),
        drainer=locks,
        reconcile_interval=3600,
    )

    runtime_task = asyncio.create_task(runtime.run())
    await polling.started.wait()
    launch_task = asyncio.create_task(
        service.launch(LaunchCommand(ProjectId("opaque-editor"), ProfileId("fake"), "sigterm"))
    )
    await terminal.started.wait()

    runtime.request_stop()
    await asyncio.sleep(0)
    assert not runtime_task.done()

    terminal.release.set()
    await launch_task
    await runtime_task

    with pytest.raises(RuntimeError, match="mutations are draining"):
        await service.launch(
            LaunchCommand(ProjectId("opaque-editor"), ProfileId("fake"), "after-stop")
        )


class InMemoryStore:
    def __init__(self) -> None:
        self.records: dict[SessionId, SessionRecord] = {}
        self.keys: set[str] = set()

    async def claim_idempotency_key(self, key: str) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    async def next_sequence(self, project_id: ProjectId, profile_id: ProfileId) -> int:
        return len(self.records) + 1

    async def save(self, record: SessionRecord) -> None:
        self.records[record.session_id] = record

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        return self.records.get(session_id)

    async def list(self) -> tuple[SessionRecord, ...]:
        return tuple(self.records.values())

    async def record_event(self, session_id: SessionId, event) -> SessionRecord:
        from remote_agents.domain.state_machine import transition

        current = self.records[session_id]
        updated = SessionRecord(
            current.session_id,
            current.project_id,
            current.profile_id,
            current.display,
            transition(current.state, event).to_state,
            current.created_at,
        )
        self.records[session_id] = updated
        return updated


class BlockingTerminal:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        self.started.set()
        await self.release.wait()
        return TerminalObservation(session_id, live=True, preserved=False)

    async def inspect(self, session_id: SessionId) -> TerminalObservation | None:
        return None

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        raise AssertionError("not used")

    async def cleanup(self, session_id: SessionId) -> None:
        raise AssertionError("not used")

    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        raise AssertionError("not used")


class BlockingPolling:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run_forever(self) -> None:
        self.started.set()
        await self.stopped.wait()

    def request_stop(self) -> None:
        self.stopped.set()


class NoopReconciler:
    async def reconcile(self, observations: tuple[object, ...]) -> None:
        return None


class FailingReconciler:
    def __init__(self) -> None:
        self.calls = 0

    async def reconcile(self, observations: tuple[object, ...]) -> None:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("synthetic reconciliation failure")


class NoopTerminal:
    async def managed_observations(self) -> tuple[object, ...]:
        return ()


class RecordingDrainer:
    def __init__(self) -> None:
        self.drained = False

    async def drain(self) -> None:
        self.drained = True


async def test_a_second_process_stops_a_session_it_never_launched(tmp_path: Path) -> None:
    """Production hands the terminal no static profiles, so the factories must carry the stop."""
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    database_path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(open_database(database_path))
    service = SessionService(store, terminal)
    command = LaunchCommand(ProjectId("opaque-editor"), ProfileId("fake"), "cross-process-stop")
    record = None
    try:
        record = await service.launch(command)
        assert record.state is SessionState.RUNNING

        other_surface = TmuxTerminal(
            gateway,
            {ProjectId("opaque-editor"): tmp_path},
            {},
            startup_timeout=STARTUP_BUDGET,
            profile_factories={
                ProfileId("fake"): lambda session_id: LaunchProfile(
                    sys.executable,
                    (sys.executable, str(tmp_path / "fake_agent.py"), "ready"),
                    {"PATH": os.environ["PATH"]},
                    "READY",
                )
            },
        )
        stopped = await SessionService(
            SQLiteSessionStore(open_database(database_path)), other_surface
        ).graceful_stop(GracefulStopCommand(record.session_id, record.profile_id))

        assert stopped.preserved
        assert (await store.get(record.session_id)).state is SessionState.ENDED
    finally:
        if record is not None:
            try:
                await gateway.mutate("kill-session", f"ra-{record.session_id}")
            except RuntimeError:
                pass
