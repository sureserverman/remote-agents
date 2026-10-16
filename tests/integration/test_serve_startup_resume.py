"""`serve` resumes the sessions a lost tmux server interrupted, once, before anything else runs.

Task 2.5 of the restart-resume plan. The snapshot is taken before the first reconcile ends the
lost records; the switch is read after it, toward off; the pass runs before the periodic tasks,
the worktree cleanup's first pass included (DEC-124); and the owner is told once.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.turn_markers import FileConversationMarks, FileTurnMarkers
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.interrupted_resumes import SQLiteInterruptedResumes
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.telegram.restart_notifications import restart_message
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.interrupted_resume import ResumeReport
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.services import SessionService
from remote_agents.composition.service import (
    ServiceComposition,
    _serve_with_reconciliation,
    build_restart_resume,
)
from remote_agents.config import TelegramSecrets
from remote_agents.domain.conversations import ProfileResumeCapability
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import TerminalObservation

_SECRETS = TelegramSecrets("token", 7, 11)
PROJECT = ProjectId("remote-agents")
CLAUDE = ProfileId("claude")
CREATED = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


class ListingTerminal(FakeTerminal):
    """The fake terminal, also listing the panes it holds, as `managed_observations` does."""

    def __init__(self) -> None:
        super().__init__()
        self.server_started = None
        self.server_unreadable = False

    async def managed_observations(self) -> tuple[TerminalObservation, ...]:
        return tuple(self._observations.values())

    async def server_started_at(self) -> datetime | None:
        if self.server_unreadable:
            raise RuntimeError("tmux said something unexpected")
        return self.server_started


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.connection = open_database(tmp_path / "sessions.sqlite3")
        self.store = SQLiteSessionStore(self.connection)
        self.worktrees = SQLiteWorktreeStore(self.connection)
        self.outcomes = SQLiteInterruptedResumes(self.connection)
        self.terminal = ListingTerminal()
        self.locks = SessionLocks()
        self.sessions = SessionService(
            self.store, self.terminal, locks=self.locks, worktrees=self.worktrees
        )
        self.activity = tmp_path / "activity"
        self.activity.mkdir()
        self.markers = FileTurnMarkers(self.activity)
        self.marks = FileConversationMarks(self.activity)
        self.switch = True
        self.notices: list[tuple[tuple[ResumeReport, ...], int]] = []
        self.events: list[str] = []
        self.snapshots: list[object] = []

    async def running(self, *, mid_turn: bool = False, mark: bool = True) -> SessionRecord:
        record = SessionRecord(
            SessionId.new(),
            PROJECT,
            CLAUDE,
            SessionDisplayIdentity(str(PROJECT), str(CLAUDE), "regular", 1),
            SessionState.RUNNING,
            CREATED,
        )
        await self.store.save(record)
        if mark:
            self.marks.record(str(record.session_id), f"conv-{record.session_id}")
        if mid_turn:
            self.markers.start(str(record.session_id), owner="claude")
        return record

    def composition(self, *, cleanup: object | None = None) -> ServiceComposition:
        async def enabled() -> bool:
            return self.switch

        async def capabilities() -> tuple[ProfileResumeCapability, ...]:
            return (ProfileResumeCapability(CLAUDE, True, True),)

        async def notify(reports: tuple[ResumeReport, ...], unchecked: int) -> None:
            self.notices.append((reports, unchecked))

        restart = build_restart_resume(
            store=self.store,
            terminal=self.terminal,
            turn_markers=self.markers,
            conversation_marks=self.marks,
            rollovers=SQLiteRolloverStore(self.connection),
            capabilities=capabilities,
            outcomes=self.outcomes,
            resume=self.sessions.resume,
            folder_of=self.worktrees.folder_for,
            enabled=enabled,
            notify=notify,
            stopping=lambda: self.locks.draining,
        )
        run = restart.resume.run

        async def spied(snapshot):
            self.snapshots.append(snapshot)
            reports = await run(snapshot)
            self.events.append("resume pass returned")
            return reports

        object.__setattr__(restart.resume, "run", spied)
        return ServiceComposition(
            build_private_bot(7, 11),
            self.terminal,
            ReconciliationService(self.store, locks=self.locks),
            restart_resume=restart,
            worktree_cleanup=cleanup,  # type: ignore[arg-type]
        )

    async def serve(self, composition: ServiceComposition, **intervals: float) -> None:
        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            await asyncio.sleep(0.05)

        await _serve_with_reconciliation(_SECRETS, composition, poll, 3600, **intervals)

    async def by_state(self, state: SessionState) -> list[SessionRecord]:
        return [record for record in await self.store.list() if record.state is state]


@pytest.fixture
def rig(tmp_path: Path):
    rig = Rig(tmp_path)
    yield rig
    rig.connection.close()


async def test_the_snapshot_is_taken_before_reconcile_ends_the_lost_records(rig: Rig) -> None:
    lost = await rig.running()

    await rig.serve(rig.composition())

    (snapshot,) = rig.snapshots
    (seen,) = snapshot.sessions  # type: ignore[attr-defined]
    assert seen.record.session_id == lost.session_id
    assert seen.record.state is SessionState.RUNNING and not seen.pane_listed
    # Reconcile then ended it, and the pass brought it back as a new session.
    assert (await rig.store.get(lost.session_id)).state is SessionState.ENDED
    (resumed,) = await rig.by_state(SessionState.RUNNING)
    assert resumed.resume_source_id == f"conv-{lost.session_id}"


async def test_the_worktree_cleanup_first_pass_starts_after_the_resume_pass_returns(
    rig: Rig,
) -> None:
    await rig.running()

    class Cleanup:
        async def pass_once(self) -> None:
            rig.events.append("cleanup pass")

    await rig.serve(rig.composition(cleanup=Cleanup()), worktree_interval=0)

    assert rig.events[0] == "resume pass returned"
    assert "cleanup pass" in rig.events


async def test_switch_off_launches_nothing_tells_nothing_and_writes_nothing(rig: Rig) -> None:
    await rig.running(mid_turn=True)
    rig.switch = False

    await rig.serve(rig.composition())

    assert await rig.by_state(SessionState.RUNNING) == []
    assert rig.notices == [] and rig.snapshots == []
    assert await rig.outcomes.outcomes() == {}
    assert rig.terminal.prompts == []


async def test_one_notice_for_n_resumes(rig: Rig) -> None:
    first = await rig.running(mid_turn=True)
    second = await rig.running()
    skipped = await rig.running(mark=False)

    await rig.serve(rig.composition())

    ((reports, unchecked),) = rig.notices
    assert unchecked == 0
    assert {report.record.session_id for report in reports} == {
        first.session_id,
        second.session_id,
        skipped.session_id,
    }
    assert len(await rig.by_state(SessionState.RUNNING)) == 2
    assert len(rig.terminal.prompts) == 1


async def test_nothing_interrupted_sends_nothing(rig: Rig) -> None:
    rig.terminal.server_started = CREATED - timedelta(days=1)  # a service-only restart
    record = await rig.running()
    await rig.terminal.launch(record.session_id, PROJECT, CLAUDE)

    await rig.serve(rig.composition())

    assert rig.notices == []
    assert await rig.outcomes.outcomes() == {}


async def test_an_unreadable_server_resumes_nothing_and_says_so(rig: Rig) -> None:
    rig.terminal.server_unreadable = True
    await rig.running()
    await rig.running()

    await rig.serve(rig.composition())

    assert rig.notices == [((), 2)]
    assert await rig.by_state(SessionState.RUNNING) == []


async def test_the_marks_of_sessions_no_longer_running_are_forgotten(rig: Rig) -> None:
    lost = await rig.running()
    alive = await rig.running()
    rig.terminal.server_started = CREATED + timedelta(hours=1)  # the console started it
    await rig.terminal.launch(alive.session_id, PROJECT, CLAUDE)

    await rig.serve(rig.composition())

    assert set(rig.marks.sessions()) == {str(alive.session_id)}
    assert rig.marks.latest(str(lost.session_id)) is None


def test_the_notice_names_how_many_came_back_and_why_the_rest_did_not() -> None:
    resumed = _report("resumed:new-1")
    not_nudged = _report("resumed:new-2; not nudged:dialog")
    skipped = _report("skipped:no-conversation", sequence=3)

    text = restart_message(
        (resumed, not_nudged, skipped), unchecked=0, project_name=lambda _: "remote-agents"
    )

    assert text.startswith("Resumed 2 sessions after restart")
    assert "claude in remote-agents #3 — no conversation was recorded for it" in text
    assert "a dialog was showing" in text


def test_the_notice_for_one_session_is_singular_and_unchecked_is_said() -> None:
    assert restart_message(
        (_report("resumed:x"),), unchecked=0, project_name=lambda _: "p"
    ).startswith("Resumed 1 session after restart")
    assert "Could not tell whether 2 sessions" in restart_message(
        (), unchecked=2, project_name=lambda _: "p"
    )
    assert restart_message((), unchecked=0, project_name=lambda _: "p") == ""


def _report(outcome: str, *, sequence: int = 1) -> ResumeReport:
    record = SessionRecord(
        SessionId.new(),
        PROJECT,
        CLAUDE,
        SessionDisplayIdentity(str(PROJECT), str(CLAUDE), "regular", sequence),
        SessionState.RUNNING,
        CREATED,
    )
    return ResumeReport(record, outcome)


def test_the_bot_composition_wires_the_restart_pass_and_its_notice(tmp_path, monkeypatch) -> None:
    from test_composition_store_wiring import _composition

    composition, _paths, connection, ui = _composition(tmp_path, monkeypatch)
    try:
        assert composition.restart_resume is not None
        assert composition.restart_notifier is composition.boundary.restart_notifier
        assert composition.restart_resume.notify is not None
    finally:
        connection.close()
        ui.close()


def test_only_the_bot_composition_builds_the_restart_pass() -> None:
    """One process resumes unattended (DEC-030): the local surface never builds or runs it."""
    src = Path(__file__).resolve().parents[2] / "src" / "remote_agents"
    allowed = {
        "build_restart_resume": {"composition/service.py", "composition/telegram.py"},
        "InterruptedResumePass(": {"composition/service.py"},
        "restart_resume=": {"composition/telegram.py"},
    }
    for needle, files in allowed.items():
        found = {
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if needle in path.read_text(encoding="utf-8")
            and not path.match("application/interrupted_resume.py")
        }
        assert found <= files, (needle, found - files)
