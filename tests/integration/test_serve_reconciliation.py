"""The service keeps durable records agreeing with observed panes while it polls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.application.activity import PaneQuietWatcher
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
from remote_agents.ports.agent_activity import HOOK_SOURCED_PROFILES, ActivityKind
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
    return ServiceComposition(build_private_bot(7, 11), terminal, ReconciliationService(store))


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


class CapturingTerminal(StubTerminal):
    """A terminal whose panes a test scripts, and which records what was asked for."""

    def __init__(self, captures: dict[str, list[str]] | None = None) -> None:
        super().__init__(())
        self.captures = captures or {}
        self.asked: list[str] = []

    async def capture(self, session_id: SessionId) -> str:
        self.asked.append(str(session_id))
        scripted = self.captures.get(str(session_id), ["unchanged"])
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]


async def _running(store: SQLiteSessionStore, profile: str) -> SessionId:
    session_id = SessionId.new()
    await store.save(
        SessionRecord(
            session_id,
            ProjectId("opaque-project"),
            ProfileId(profile),
            SessionDisplayIdentity("opaque-project", profile, "regular", 1),
            SessionState.STARTING,
            datetime.now(UTC),
        )
    )
    await store.record_event(session_id, LifecycleEvent.READY)
    return session_id


async def test_activity_watching_skips_the_profiles_that_report_for_themselves(
    tmp_path: Path,
) -> None:
    """A session with a hook is already telling us; watching it too would say it twice.

    **Every** member of `HOOK_SOURCED_PROFILES` is enrolled, derived from the frozenset rather
    than named here, and that indirection is the point rather than tidiness. This subtraction
    is now the whole of the rule that `quiet` reaches the profiles with quiet fallback — Codex
    remains watched until a reported hook event suppresses that one spell, while `opencode` and
    `cursor-agent` have only pane evidence. A hook-exclusive session watched as well would tell
    the owner the same thing twice, once as a report and once as a guess. Written as a
    hand-copied pair, this test would keep passing for `claude` and `claude-remote` while a
    profile added to the frozenset later went unwatched *and* unasserted.
    """
    with open_database(tmp_path / "state.db") as connection:
        store = SQLiteSessionStore(connection)
        assert HOOK_SOURCED_PROFILES, "a frozenset that emptied would make this test vacuous"
        hooked = {profile: str(await _running(store, profile)) for profile in HOOK_SOURCED_PROFILES}
        watched = await _running(store, "codex")
        terminal = CapturingTerminal()
        watcher = PaneQuietWatcher(store, terminal.capture, quiet_polls=2)

        await watcher.poll()

        assert terminal.asked == [str(watched)]
        for profile, session_id in hooked.items():
            assert session_id not in terminal.asked, f"{profile} reports for itself"


async def test_activity_watching_survives_a_capture_that_raises(tmp_path: Path) -> None:
    """A pane that cannot be read is not a pane that has gone quiet."""
    with open_database(tmp_path / "state.db") as connection:
        store = SQLiteSessionStore(connection)
        await _running(store, "codex")

        async def refuse(session_id: SessionId) -> str:
            raise RuntimeError("tmux command failed")

        watcher = PaneQuietWatcher(store, refuse, quiet_polls=1)

        assert await watcher.poll() == ()
        assert await watcher.poll() == ()


async def test_activity_watching_reports_a_codex_pane_that_stopped_changing(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "state.db") as connection:
        store = SQLiteSessionStore(connection)
        session_id = await _running(store, "codex")
        terminal = CapturingTerminal({str(session_id): ["one", "two", "two", "two"]})
        watcher = PaneQuietWatcher(store, terminal.capture, quiet_polls=2)

        assert await watcher.poll() == ()
        assert await watcher.poll() == ()
        assert await watcher.poll() == ()
        (activity,) = await watcher.poll()

        assert activity.kind is ActivityKind.QUIET
        assert activity.session_id == str(session_id)


async def test_activity_watching_forgets_a_session_that_is_no_longer_running(
    tmp_path: Path,
) -> None:
    """The watch map is per-session state, and a service runs for weeks."""
    with open_database(tmp_path / "state.db") as connection:
        store = SQLiteSessionStore(connection)
        session_id = await _running(store, "codex")
        terminal = CapturingTerminal()
        watcher = PaneQuietWatcher(store, terminal.capture, quiet_polls=2)
        await watcher.poll()
        assert watcher._watches

        await store.record_event(session_id, LifecycleEvent.RECONCILED_PANE_DEAD)

        await watcher.poll()

        assert watcher._watches == {}


async def test_activity_watching_runs_beside_the_poll_and_stops_with_it(tmp_path: Path) -> None:
    """The watcher is a second periodic task, and shutdown must not leave it pending.

    Cancelling one loop and forgetting the other is the shape of bug that leaves a task
    holding a database connection open past the connection's own lifetime.
    """
    with open_database(tmp_path / "state.db") as connection:
        store = SQLiteSessionStore(connection)
        session_id = await _running(store, "codex")
        terminal = CapturingTerminal()
        # Reconciliation runs first and ends any record whose pane the terminal does not
        # report, so a stub with no observations would delete the very session under watch.
        terminal.observations = (TerminalObservation(session_id, live=True, preserved=False),)
        watcher = PaneQuietWatcher(store, terminal.capture, quiet_polls=1)
        composition = ServiceComposition(
            build_private_bot(7, 11), terminal, ReconciliationService(store), watcher
        )

        async def poll_briefly(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            await asyncio.sleep(0.05)

        before = len(asyncio.all_tasks())
        await _serve_with_reconciliation(
            _SECRETS, composition, poll_briefly, 0.01, activity_interval=0.01
        )

        assert terminal.asked, "the watcher never polled while the service was serving"
        await asyncio.sleep(0)
        assert len(asyncio.all_tasks()) <= before


async def test_activity_watching_never_takes_the_service_down(tmp_path: Path) -> None:
    """A watch pass that raises is logged and the next one still happens."""
    with open_database(tmp_path / "state.db") as connection:
        store = SQLiteSessionStore(connection)
        passes = 0

        class ExplodingWatcher(PaneQuietWatcher):
            async def poll(self):  # type: ignore[override]
                nonlocal passes
                passes += 1
                raise RuntimeError("the watcher itself failed")

        terminal = CapturingTerminal()
        composition = ServiceComposition(
            build_private_bot(7, 11),
            terminal,
            ReconciliationService(store),
            ExplodingWatcher(store, terminal.capture, quiet_polls=1),
        )

        async def poll_briefly(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            await asyncio.sleep(0.06)

        await _serve_with_reconciliation(
            _SECRETS, composition, poll_briefly, 0.01, activity_interval=0.01
        )

        assert passes >= 2, f"the loop did not survive its own failure: {passes} pass(es)"


async def test_activity_watching_gives_up_on_a_capture_that_never_returns(
    tmp_path: Path,
) -> None:
    """A hang is the one capture failure the existing guard cannot catch.

    `except Exception` only fires on something raised. The tmux runner awaits `communicate()`
    with no timeout, so a wedged server leaves the capture awaiting forever: the watch loop
    stops for the life of the process, every watched session frozen, and nothing logged. The
    bound turns a permanent silent stall into one skipped pass.
    """
    with open_database(tmp_path / "state.db") as connection:
        store = SQLiteSessionStore(connection)
        await _running(store, "codex")

        async def never_returns(session_id: SessionId) -> str:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        watcher = PaneQuietWatcher(store, never_returns, quiet_polls=2)
        watcher._capture_timeout = 0.05

        assert await asyncio.wait_for(watcher.poll(), timeout=5) == ()
