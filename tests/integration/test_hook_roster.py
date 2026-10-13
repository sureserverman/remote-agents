"""The service keeps a roster of its live sessions for each hook whose payload names its session.

A Codex hook runs in a daemon that carries whichever pane started it, so it names its session
from the payload against this roster instead (DEC-126). These tests drive the real SQLite store,
the real session-folder lookup and the real file writer: the roster the hook reads is the file
written here, not a fixture of it.
"""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.adapters.agents.hook_identity import (
    FileHookRoster,
    RosterEntry,
    read_roster,
    roster_path,
)
from remote_agents.adapters.agents.registry import profiles_named_by_payload
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.hook_roster import HookRosterPublisher
from remote_agents.application.services import SessionService
from remote_agents.application.session_folders import SessionFolders
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

PROJECT = ProjectId("editor")
CODEX = ProfileId("codex")
CLAUDE = ProfileId("claude")
_RESUMED_THREAD = "01a1360a-d366-7913-abdc-27aa12faf338"


class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        connection = open_database(tmp_path / "sessions.sqlite3")
        self.store = SQLiteSessionStore(connection)
        self.project = tmp_path / "editor"
        self.project.mkdir()
        self.spool = tmp_path / "activity"
        self.spool.mkdir(mode=0o700)
        folders = SessionFolders({PROJECT: self.project}, SQLiteWorktreeStore(connection))
        self.publisher = HookRosterPublisher(
            self.store,
            folders.folder_for,
            profiles_named_by_payload(),
            FileHookRoster(self.spool),
        )

    def roster(self, hook: str = "codex") -> tuple[RosterEntry, ...] | None:
        return read_roster(roster_path(self.spool, hook))

    async def record(
        self,
        profile: ProfileId = CODEX,
        state: SessionState = SessionState.RUNNING,
        resumed: str | None = None,
    ) -> SessionRecord:
        record = SessionRecord(
            SessionId.new(),
            PROJECT,
            profile,
            SessionDisplayIdentity("editor", str(profile), "regular", 1),
            state,
            datetime(2026, 10, 13, 16, 14, tzinfo=UTC),
            resume_profile_id=None if resumed is None else profile,
            resume_source_id=resumed,
        )
        await self.store.save(record)
        return record


class _Terminal:
    """Reads the roster at the moment the pane would start, which is what the hook will read."""

    def __init__(self, rig: _Rig) -> None:
        self.rig = rig
        self.seen_at_launch: tuple[RosterEntry, ...] | None = None

    async def launch(self, session_id, project_id, profile_id, *, remote_control):  # type: ignore[no-untyped-def]
        self.seen_at_launch = self.rig.roster()
        return TerminalObservation(session_id, True, False)


def test_only_codex_names_its_session_from_the_payload() -> None:
    named = profiles_named_by_payload()

    assert named.get(str(CODEX)) == "codex"
    assert str(CLAUDE) not in named


async def test_a_launch_is_on_the_roster_before_its_pane_starts(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    terminal = _Terminal(rig)
    service = SessionService(rig.store, terminal, before_pane=rig.publisher.publish)  # type: ignore[arg-type]

    outcome = await service.launch(LaunchCommand(PROJECT, CODEX, "k1"))

    assert terminal.seen_at_launch is not None
    (entry,) = terminal.seen_at_launch
    assert entry.session_id == str(outcome.record.session_id)
    assert entry.workspace == str(rig.project.resolve())
    assert entry.created_at == outcome.record.created_at
    assert entry.resume_source_id is None


async def test_a_resumed_session_carries_its_conversation(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    resumed = await rig.record(resumed=_RESUMED_THREAD)

    await rig.publisher.publish()

    assert rig.roster() == (
        RosterEntry(
            str(resumed.session_id),
            str(rig.project.resolve()),
            resumed.created_at,
            _RESUMED_THREAD,
        ),
    )


async def test_an_ended_session_leaves_the_roster_on_the_next_pass(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    live = await rig.record()
    await rig.record(state=SessionState.ENDED)
    await rig.publisher.publish()
    assert [entry.session_id for entry in rig.roster() or ()] == [str(live.session_id)]

    await rig.store.record_event(live.session_id, LifecycleEvent.CLEANUP_CONFIRMED)
    await rig.publisher.publish()

    assert rig.roster() == ()


async def test_the_roster_is_private_and_another_providers_sessions_stay_off_it(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    await rig.record(profile=CLAUDE)

    await rig.publisher.publish()

    path = roster_path(rig.spool, "codex")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rig.roster() == ()
    assert rig.roster("claude") is None
