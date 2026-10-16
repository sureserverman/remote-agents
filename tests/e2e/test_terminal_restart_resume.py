"""A lost tmux server, on a real socket: `serve` brings each session back once, in its conversation.

Task 3.3 of the restart-resume plan (DEC-127). Two harmless fake-agent sessions run on an isolated
tmux server, one of them mid-turn. `serve` starts and stops once with nothing lost, the server is
killed, and `serve` starts again over the real SQLite stores, the real `SessionService` and the
real `TmuxTerminal`: the snapshot, the reconcile, the resume launch, the guarded send's idle
check, the paste and the `Enter` are all the production ones. The fake agent draws a one-line
composer (a rule, then `❯ `) and echoes each line it is given as `GOT: <line>`, so the nudge is
read off the resumed pane's own screen. Run alone: real-tmux tests fail spuriously beside
another uv/pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.agents.turn_markers import FileConversationMarks, FileTurnMarkers
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
PROFILE = ProfileId("fake")

#: The agent draws its composer this long after its readiness marker -- which is when the resume
#: returns -- so the first try of the nudge meets a screen with no composer on it yet.
BOOT_DELAY = 1.0

#: How long `serve` is kept polling while the pass resumes and nudges. The nudge retries every
#: 3 s and the composer is up after `BOOT_DELAY`, so it lands well inside this.
PASS_BUDGET = 30.0

_AGENT = f"""
import time
print("READY", flush=True)
time.sleep({BOOT_DELAY})
while True:
    print("─" * 24, flush=True)
    try:
        line = input("❯ ")
    except EOFError:
        break
    print("GOT: " + line, flush=True)
"""

_COMPOSER = ComposerScreen(composer=r"^─{10,}\n❯ ?(?P<draft>[^\n]*)\Z")


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        agent = tmp_path / "fake_agent.py"
        agent.write_text(_AGENT, encoding="utf-8")
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

        self.terminal = TmuxTerminal(
            gateway,
            {PROJECT: tmp_path},
            {
                PROFILE: LaunchProfile(
                    sys.executable, (sys.executable, str(agent)), environment, "READY"
                )
            },
            startup_timeout=10.0,
            resume_profile_factories={PROFILE: resume_profile},
            composers={"fake": ProviderDescriptor(PROFILE, "F", "Fake", composer=_COMPOSER)},
        )
        self.connection = open_database(tmp_path / "sessions.sqlite3")
        self.store = SQLiteSessionStore(self.connection)
        self.worktrees = SQLiteWorktreeStore(self.connection)
        self.outcomes = SQLiteInterruptedResumes(self.connection)
        self.locks = SessionLocks()
        activity = tmp_path / "activity"
        activity.mkdir()
        self.markers = FileTurnMarkers(activity)
        self.marks = FileConversationMarks(activity)
        self.switch = True
        self.notices: list[tuple[tuple[ResumeReport, ...], int]] = []
        self.passes = 0

    async def launched(self, sequence: int) -> SessionRecord:
        """A fake-agent session launched on the real server, then reconciled to RUNNING."""
        session_id = SessionId.new()
        await self.store.save(
            SessionRecord(
                session_id,
                PROJECT,
                PROFILE,
                SessionDisplayIdentity(str(PROJECT), str(PROFILE), "regular", sequence),
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
        """A fresh process's composition. The locks are new each time: a `serve` that stopped
        drained its own, and a restarted process never inherits that shut gate."""
        self.locks = locks = SessionLocks()
        sessions = SessionService(self.store, self.terminal, locks=locks, worktrees=self.worktrees)

        async def enabled() -> bool:
            return self.switch

        async def capabilities() -> tuple[ProfileResumeCapability, ...]:
            return (ProfileResumeCapability(PROFILE, True, True),)

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
            resume=sessions.resume,
            folder_of=self.worktrees.folder_for,
            enabled=enabled,
            notify=notify,
            stopping=lambda: locks.draining,
        )
        run = restart.resume.run

        async def counted(snapshot):
            try:
                return await run(snapshot)
            finally:
                self.passes += 1

        object.__setattr__(restart.resume, "run", counted)
        return ServiceComposition(
            build_private_bot(7, 11),
            self.terminal,
            ReconciliationService(self.store, locks=locks),
            restart_resume=restart,
            drain=locks.drain,
        )

    async def serve(self, until: Callable[[], bool], *, budget: float = PASS_BUDGET) -> None:
        """One `serve` run, polling until `until()` holds or `budget` runs out, then stopping."""

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            deadline = asyncio.get_running_loop().time() + budget
            while not until() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)

        await _serve_with_reconciliation(_SECRETS, self.composition(), poll, 3600)

    async def kill_server(self) -> None:
        """Kill the server and wait until it is gone, as it is once a reboot has finished.

        `kill-server` returns before the server has exited, and a `list-sessions` in that moment
        answers "server exited unexpectedly", which the gateway does not read as absent -- so
        a start that raced it would see an unreadable server and resume nothing (DEC-127)."""
        try:
            await AsyncTmuxRunner().run("tmux", "-L", self.socket, "kill-server")
        except RuntimeError:
            pass  # no server to kill
        for _ in range(50):
            try:
                if await self.terminal.server_started_at() is None:
                    return
            except RuntimeError:
                pass
            await asyncio.sleep(0.1)
        raise AssertionError("the tmux server did not go away")

    async def running(self) -> list[SessionRecord]:
        return [
            record for record in await self.store.list() if record.state is SessionState.RUNNING
        ]

    def intent(self, session_id: SessionId) -> str:
        return (self.intents / f"{session_id}.json").read_text(encoding="utf-8")


@pytest.fixture
async def rig(tmp_path: Path):
    rig = Rig(tmp_path)
    try:
        yield rig
    finally:
        rig.connection.close()
        await rig.kill_server()


async def _screen_settled(rig: Rig, session_id: SessionId, expected: str) -> str:
    screen = ""
    for _ in range(50):
        screen = await rig.terminal.capture(session_id)
        if expected in screen:
            return screen
        await asyncio.sleep(0.1)
    return screen


async def test_a_killed_server_resumes_each_session_once_and_nudges_the_mid_turn_one(
    rig: Rig,
) -> None:
    idle = await rig.launched(1)
    mid_turn = await rig.launched(2)
    rig.marks.record(str(idle.session_id), "conv-idle")
    rig.marks.record(str(mid_turn.session_id), "conv-mid-turn")

    # A service-only restart first: the panes are alive, so nothing is lost and nothing is said.
    await rig.serve(until=lambda: rig.passes == 1)
    assert rig.passes == 1, "the pass judged the start; a silent run proves nothing"
    assert rig.notices == []
    assert await rig.outcomes.outcomes() == {}
    assert {record.session_id for record in await rig.running()} == {
        idle.session_id,
        mid_turn.session_id,
    }

    # A turn starts in one of them, and the server goes with every pane on it.
    rig.markers.start(str(mid_turn.session_id), owner="fake")
    await rig.kill_server()

    await rig.serve(until=lambda: bool(rig.notices))

    assert rig.passes == 2
    running = await rig.running()
    sources = sorted(str(record.resume_source_id) for record in running)
    assert sources == ["conv-idle", "conv-mid-turn"], "one session per conversation, no more"
    resumed = {record.resume_source_id: record for record in running}
    listed = {observation.session_id for observation in await rig.terminal.managed_observations()}
    assert {record.session_id for record in resumed.values()} <= listed
    assert '"--resume", "conv-idle"' in rig.intent(resumed["conv-idle"].session_id)
    assert '"--resume", "conv-mid-turn"' in rig.intent(resumed["conv-mid-turn"].session_id)
    for lost in (idle, mid_turn):
        ended = await rig.store.get(lost.session_id)
        assert ended is not None and ended.state is SessionState.ENDED

    nudged = await _screen_settled(rig, resumed["conv-mid-turn"].session_id, f"GOT: {NUDGE}")
    assert nudged.count(f"GOT: {NUDGE}") == 1, nudged
    await asyncio.sleep(0.5)
    assert "GOT:" not in await rig.terminal.capture(resumed["conv-idle"].session_id)

    ((reports, unchecked),) = rig.notices
    assert unchecked == 0
    assert {report.record.session_id for report in reports} == {
        idle.session_id,
        mid_turn.session_id,
    }
    assert all(report.outcome.startswith("resumed:") for report in reports), reports
    assert "; not nudged:" not in "".join(report.outcome for report in reports)
    notice = restart_message(reports, unchecked=unchecked, project_name=str)
    assert notice.startswith("Resumed 2 sessions after restart"), notice
    outcomes = await rig.outcomes.outcomes()
    assert set(outcomes.values()) == {
        f"resumed:{resumed['conv-idle'].session_id}",
        f"resumed:{resumed['conv-mid-turn'].session_id}",
    }

    # A third start over the same store, the resumed panes alive: a restart never acts twice.
    await rig.serve(until=lambda: rig.passes == 3)
    assert rig.passes == 3
    again = await rig.running()
    assert {record.session_id for record in again} == {r.session_id for r in running}
    assert await rig.outcomes.outcomes() == outcomes
    assert len(rig.notices) == 1
    screen = await rig.terminal.capture(resumed["conv-mid-turn"].session_id)
    assert screen.count(f"GOT: {NUDGE}") == 1, screen


async def test_with_the_switch_off_a_killed_server_brings_nothing_back(rig: Rig) -> None:
    first = await rig.launched(1)
    second = await rig.launched(2)
    rig.marks.record(str(first.session_id), "conv-first")
    rig.marks.record(str(second.session_id), "conv-second")
    rig.markers.start(str(first.session_id), owner="fake")
    await rig.kill_server()
    rig.switch = False

    # Nothing to wait for, so the run is given a fixed second to do anything it would.
    await rig.serve(until=lambda: rig.passes > 0, budget=1.0)

    assert await rig.running() == []
    assert rig.notices == [] and rig.passes == 0
    assert await rig.outcomes.outcomes() == {}
    assert await rig.terminal.server_started_at() is None, "nothing was launched"
