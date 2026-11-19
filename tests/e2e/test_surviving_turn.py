"""A lost tmux server under a turn, on a real socket: what the resumed pane shows first decides
whether the nudge is typed (BL-137, Task 3.3).

One fake-Codex session (`tests/support/fake_codex_screens.py`) is launched on an isolated tmux
server and marked mid-turn; the server is killed and `serve` starts over the real SQLite stores,
`SessionService` and `TmuxTerminal`. The resume draws one of the three first looks Codex 0.170.0
was measured drawing (`docs/acceptance-2026-11-surviving-codex-turn.md`), read through Codex's
own busy line and title-spinner patterns:

- a surviving turn's busy line, gone 3 s later: no key reaches the pane, ever;
- a boot's title spinner over an idle composer for 5 s: the nudge is typed once, after it;
- an idle composer at once: the nudge is typed once.

Run alone: real-tmux tests fail spuriously beside another uv/pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import fake_codex_screens
import pytest

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.agents.turn_markers import (
    FileConversationMarks,
    FileStartedMarks,
    FileTurnMarkers,
)
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.interrupted_resumes import SQLiteInterruptedResumes
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.telegram.restart_notifications import restart_message
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.interrupted_resume import ResumeReport
from remote_agents.application.limit_resume import NUDGE
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.services import SessionService
from remote_agents.composition.service import (
    ServiceComposition,
    _serve_with_reconciliation,
    build_restart_resume,
)
from remote_agents.config import TelegramSecrets
from remote_agents.domain.conversations import ProfileResumeCapability, ProviderConversationId
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor

_SECRETS = TelegramSecrets("token", 7, 11)
PROJECT = ProjectId("opaque-editor")
PROFILE = ProfileId("fake-codex")
#: Long enough for the boot's 5 s spinner and the nudge's 3 s retries after it.
PASS_BUDGET = 30.0
#: How long a screen that must stay clear of any typed key is watched after the turn ends.
QUIET_SECONDS = 4.0


def _codex_screen() -> ComposerScreen:
    """The fake agent's one-line composer, read with Codex's own busy, running-turn and title
    patterns."""
    codex = next(d for d in provider_descriptors() if str(d.profile_id) == "codex").composer
    assert codex is not None and codex.busy and codex.busy_title and codex.running_turn
    return ComposerScreen(
        composer=r"^─{10,}\n❯ ?(?P<draft>[^\n]*)\Z",
        busy=codex.busy,
        busy_title=codex.busy_title,
        running_turn=codex.running_turn,
    )


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        agent = Path(fake_codex_screens.__file__)
        self.intents = tmp_path / "intents"
        self.socket = f"remote-agents-test-{uuid4().hex}"
        gateway = TmuxGateway(self.socket, AsyncTmuxRunner(), intent_directory=self.intents)
        environment = {"PATH": os.environ["PATH"]}

        def resume_profile(_session: SessionId, source: ProviderConversationId) -> LaunchProfile:
            return LaunchProfile(
                sys.executable,
                (sys.executable, str(agent), "--resume", source.value),
                environment,
                "READY",
            )

        launch = LaunchProfile(sys.executable, (sys.executable, str(agent)), environment, "READY")
        self.terminal = TmuxTerminal(
            gateway,
            {PROJECT: tmp_path},
            {PROFILE: launch},
            startup_timeout=10.0,
            resume_profile_factories={PROFILE: resume_profile},
            composers={
                str(PROFILE): ProviderDescriptor(
                    PROFILE, "C", "Fake Codex", composer=_codex_screen()
                )
            },
        )
        self.connection = open_database(tmp_path / "sessions.sqlite3")
        self.store = SQLiteSessionStore(self.connection)
        self.worktrees = SQLiteWorktreeStore(self.connection)
        self.outcomes = SQLiteInterruptedResumes(self.connection)
        activity = tmp_path / "activity"
        activity.mkdir()
        self.markers = FileTurnMarkers(activity)
        self.marks = FileConversationMarks(activity)
        self.started = FileStartedMarks(activity)
        self.notices: list[tuple[tuple[ResumeReport, ...], int]] = []
        self.sends: list[str] = []

    async def launched(self) -> SessionRecord:
        session_id = SessionId.new()
        await self.store.save(
            SessionRecord(
                session_id,
                PROJECT,
                PROFILE,
                SessionDisplayIdentity(str(PROJECT), str(PROFILE), "regular", 1),
                SessionState.STARTING,
                datetime.now(UTC),
            )
        )
        assert (await self.terminal.launch(session_id, PROJECT, PROFILE)).live
        await ReconciliationService(self.store, settle_after=timedelta(0)).reconcile(
            await self.terminal.managed_observations()
        )
        record = await self.store.get(session_id)
        assert record is not None and record.state is SessionState.RUNNING
        return record

    def composition(self) -> ServiceComposition:
        locks = SessionLocks()
        sessions = SessionService(self.store, self.terminal, locks=locks, worktrees=self.worktrees)
        send = self.terminal.send_prompt

        async def counted(session_id: SessionId, text: str):
            delivery = await send(session_id, text)
            self.sends.append(f"{delivery.outcome.value}:{delivery.reason}")
            return delivery

        self.terminal.send_prompt = counted  # type: ignore[method-assign]

        async def enabled() -> bool:
            return True

        async def capabilities() -> tuple[ProfileResumeCapability, ...]:
            return (ProfileResumeCapability(PROFILE, True, True),)

        async def notify(reports: tuple[ResumeReport, ...], unchecked: int) -> None:
            self.notices.append((reports, unchecked))

        restart = build_restart_resume(
            store=self.store,
            terminal=self.terminal,
            turn_markers=self.markers,
            conversation_marks=self.marks,
            started_marks=self.started,
            rollovers=SQLiteRolloverStore(self.connection),
            capabilities=capabilities,
            outcomes=self.outcomes,
            resume=sessions.resume,
            folder_of=self.worktrees.folder_for,
            enabled=enabled,
            notify=notify,
            stopping=lambda: locks.draining,
        )
        return ServiceComposition(
            build_private_bot(7, 11),
            self.terminal,
            ReconciliationService(self.store, locks=locks),
            restart_resume=restart,
            drain=locks.drain,
        )

    async def serve(self, until: Callable[[], bool]) -> None:
        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            deadline = asyncio.get_running_loop().time() + PASS_BUDGET
            while not until() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)

        await _serve_with_reconciliation(_SECRETS, self.composition(), poll, 3600)

    async def kill_server(self) -> None:
        """Kill the server and wait until it is gone (see `test_terminal_restart_resume`)."""
        try:
            await AsyncTmuxRunner().run("tmux", "-L", self.socket, "kill-server")
        except RuntimeError:
            pass
        for _ in range(50):
            try:
                if await self.terminal.server_started_at() is None:
                    return
            except RuntimeError:
                pass
            await asyncio.sleep(0.1)
        raise AssertionError("the tmux server did not go away")

    async def resumed(self) -> SessionRecord:
        (record,) = [
            record for record in await self.store.list() if record.state is SessionState.RUNNING
        ]
        return record


@pytest.fixture
async def rig(tmp_path: Path):
    rig = Rig(tmp_path)
    try:
        yield rig
    finally:
        rig.connection.close()
        await rig.kill_server()


async def _lost_mid_turn(rig: Rig, conversation: str) -> None:
    """One session, its conversation marked and a turn under way, and the server killed."""
    lost = await rig.launched()
    rig.marks.record(str(lost.session_id), conversation)
    rig.markers.start(str(lost.session_id), owner="fake-codex")
    await rig.kill_server()


async def _screen_holding(rig: Rig, session_id: SessionId, text: str) -> str:
    screen = ""
    for _ in range(100):
        screen = await rig.terminal.capture(session_id)
        if text in screen:
            return screen
        await asyncio.sleep(0.1)
    return screen


async def test_a_turn_running_on_screen_at_the_first_look_gets_no_keys_at_all(rig: Rig) -> None:
    await _lost_mid_turn(rig, "conv-survivor")

    await rig.serve(until=lambda: bool(rig.notices))

    ((reports, _unchecked),) = rig.notices
    (report,) = reports
    resumed = await rig.resumed()
    assert report.outcome == f"resumed:{resumed.session_id}; not nudged:turn_survived"
    assert rig.sends == ["refused:busy_on_screen"], rig.sends
    # The turn ends 3 s after the resume; the pane is watched past that for any key at all.
    ended = await _screen_holding(rig, resumed.session_id, "• ok")
    assert "• ok" in ended, ended
    await asyncio.sleep(QUIET_SECONDS)
    screen = await rig.terminal.capture(resumed.session_id)
    assert NUDGE not in screen and "GOT:" not in screen, screen
    notice = restart_message(reports, unchecked=0, project_name=str)
    assert "its turn was still running" in notice, notice


async def test_a_title_only_busy_first_look_is_a_boot_and_is_nudged_once(rig: Rig) -> None:
    await _lost_mid_turn(rig, "conv-boot")

    await rig.serve(until=lambda: bool(rig.notices))

    ((reports, _unchecked),) = rig.notices
    (report,) = reports
    resumed = await rig.resumed()
    assert report.outcome == f"resumed:{resumed.session_id}", report.outcome
    assert rig.sends[0] == "refused:busy", rig.sends
    assert rig.sends[-1] == "sent:None", rig.sends
    nudged = await _screen_holding(rig, resumed.session_id, f"GOT: {NUDGE}")
    assert nudged.count(f"GOT: {NUDGE}") == 1, nudged


async def test_an_idle_first_look_is_nudged_once(rig: Rig) -> None:
    await _lost_mid_turn(rig, "conv-idle")

    await rig.serve(until=lambda: bool(rig.notices))

    ((reports, _unchecked),) = rig.notices
    (report,) = reports
    resumed = await rig.resumed()
    assert report.outcome == f"resumed:{resumed.session_id}", report.outcome
    nudged = await _screen_holding(rig, resumed.session_id, f"GOT: {NUDGE}")
    assert nudged.count(f"GOT: {NUDGE}") == 1, nudged
