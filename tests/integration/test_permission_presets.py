"""Persist the requested launch preset before any managed process is started."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tmux.profiles import build_launch_profile, build_resume_profile
from remote_agents.application.commands import LaunchCommand, ResumeCommand
from remote_agents.application.permission_presets import PermissionSettings, SessionPermissions
from remote_agents.application.services import SessionService
from remote_agents.composition.limits_source import ConfigPermissionSetting
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import PermissionPreset, closed_profiles

PROJECT = ProjectId("fixture-project")
CODEX = ProfileId("codex")
DEFINITION = next(p for p in closed_profiles() if p.profile_id == CODEX)
EXPECTED = {
    PermissionPreset.APPROVE_FOR_ME: ("--approve-for-me",),
    PermissionPreset.ASK: (
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "on-request",
        "-c",
        'approvals_reviewer="user"',
    ),
    PermissionPreset.FULL_ACCESS: ("--dangerously-bypass-approvals-and-sandbox",),
    PermissionPreset.PROVIDER_DEFAULT: (),
}


def conversation(source="source-123"):
    return ResolvedConversation(
        ConversationSummary(
            ConversationReference("c-" + "a" * 20),
            CODEX,
            PROJECT,
            ConversationState.RESUMABLE,
            datetime.now(UTC),
        ),
        ProviderConversationId(source),
    )


class CapturingTerminal(FakeTerminal):
    def __init__(self, bindings):
        super().__init__()
        self.bindings = bindings
        self.argv = []
        self.presets = []

    async def launch(
        self,
        session_id,
        project_id,
        profile_id,
        *,
        remote_control=False,
        permissions=None,
        record_launch=True,
    ):
        if record_launch:
            assert await self.bindings.for_session(session_id) == permissions
            self.presets.append(permissions)
            profile = build_launch_profile(
                DEFINITION,
                Path("/fixture/codex"),
                session_id,
                {},
                permissions=permissions,
            )
            self.argv.append(profile.argv)
        return await super().launch(
            session_id,
            project_id,
            profile_id,
            remote_control=remote_control,
            record_launch=record_launch,
        )

    async def resume(self, session_id, project_id, profile_id, source_id, *, permissions=None):
        assert await self.bindings.for_session(session_id) == permissions
        self.presets.append(permissions)
        profile = build_resume_profile(
            DEFINITION,
            Path("/fixture/codex"),
            session_id,
            source_id,
            {},
            permissions=permissions,
        )
        self.argv.append(profile.argv)
        return await super().resume(session_id, project_id, profile_id, source_id)


class Rig:
    def __init__(self, tmp_path):
        self.connection = open_database(tmp_path / "sessions.sqlite3")
        self.store = SQLiteSessionStore(self.connection)
        self.bindings = SQLitePermissionBindings(self.connection)
        self.path = tmp_path / "config.toml"
        if not self.path.exists():
            self.path.write_text('[codex]\ndefault_permissions = "approve_for_me"\n')
        self.settings = PermissionSettings(
            ConfigPermissionSetting(self.path),
            profiles={CODEX: tuple(PermissionPreset)},
        )
        self.permissions = SessionPermissions(self.settings, self.bindings, self.store)
        self.terminal = CapturingTerminal(self.bindings)
        self.service = SessionService(
            self.store,
            self.terminal,
            permissions=self.permissions,
        )


@pytest.fixture
def rig(tmp_path):
    value = Rig(tmp_path)
    yield value
    value.connection.close()


@pytest.mark.parametrize("preset", list(PermissionPreset))
@pytest.mark.parametrize("resume", [False, True])
async def test_every_preset_is_snapshotted_before_fresh_and_resumed_starts(rig, preset, resume):
    await rig.settings.set(preset)
    command = (
        ResumeCommand(PROJECT, CODEX, conversation(), "resume")
        if resume
        else LaunchCommand(PROJECT, CODEX, "launch")
    )
    result = await (rig.service.resume(command) if resume else rig.service.launch(command))
    assert rig.terminal.argv == [
        ("/fixture/codex", *(["resume", "source-123"] if resume else []), *EXPECTED[preset])
    ]
    assert await rig.bindings.for_session(result.record.session_id) == preset


async def test_explicit_override_wins_without_changing_the_default(rig):
    result = await rig.service.launch(
        LaunchCommand(
            PROJECT,
            CODEX,
            "explicit",
            permissions=PermissionPreset.ASK,
        )
    )
    assert await rig.bindings.for_session(result.record.session_id) == PermissionPreset.ASK
    assert await rig.settings.read() == PermissionPreset.APPROVE_FOR_ME


@pytest.mark.parametrize("preset", list(PermissionPreset))
async def test_continuation_keeps_its_snapshot_after_default_changes_and_database_reopen(
    tmp_path,
    preset,
):
    first = Rig(tmp_path)
    try:
        original = await first.service.launch(
            LaunchCommand(
                PROJECT,
                CODEX,
                "original",
                permissions=preset,
            )
        )
        await first.settings.set(
            PermissionPreset.FULL_ACCESS
            if preset != PermissionPreset.FULL_ACCESS
            else PermissionPreset.ASK
        )
    finally:
        first.connection.close()
    second = Rig(tmp_path)
    try:
        successor = await second.service.launch(
            LaunchCommand(
                PROJECT,
                CODEX,
                "continuation",
                inherit_permissions_of=original.record.session_id,
            )
        )
        assert await second.bindings.for_session(successor.record.session_id) == preset
        assert second.terminal.presets == [preset]
    finally:
        second.connection.close()


async def test_explicit_override_wins_over_inherited_preset(rig):
    original = await rig.service.launch(LaunchCommand(PROJECT, CODEX, "first"))
    result = await rig.service.launch(
        LaunchCommand(
            PROJECT,
            CODEX,
            "second",
            permissions=PermissionPreset.ASK,
            inherit_permissions_of=original.record.session_id,
        )
    )
    assert await rig.bindings.for_session(result.record.session_id) == PermissionPreset.ASK


async def test_invalid_or_unsupported_override_starts_nothing(rig):
    with pytest.raises(ValueError, match="permission preset"):
        LaunchCommand(PROJECT, CODEX, "invalid", permissions="invented")
    with pytest.raises(ValueError, match="permission"):
        await rig.service.launch(
            LaunchCommand(
                PROJECT,
                ProfileId("claude"),
                "unsupported",
                permissions=PermissionPreset.FULL_ACCESS,
            )
        )
    assert await rig.store.list() == ()
    assert rig.terminal.argv == []


async def test_preset_and_session_save_roll_back_together(rig):
    rig.connection.execute("""CREATE TRIGGER refuse_preset BEFORE INSERT ON session_permissions
        BEGIN SELECT RAISE(ABORT, 'fixture write refused'); END""")
    with pytest.raises(Exception, match="fixture write refused"):
        await rig.service.launch(LaunchCommand(PROJECT, CODEX, "failed-save"))
    assert await rig.store.list() == ()
    assert rig.terminal.argv == []


async def test_replayed_resume_returns_the_original_snapshot_and_starts_no_second_pane(rig):
    command = ResumeCommand(PROJECT, CODEX, conversation(), "resume")
    original = await rig.service.resume(command)
    await rig.settings.set(PermissionPreset.FULL_ACCESS)
    replay = await rig.service.resume(command)
    assert replay.record.session_id == original.record.session_id
    assert not replay.created
    assert rig.terminal.presets == [PermissionPreset.APPROVE_FOR_ME]


@pytest.mark.parametrize("preset", list(PermissionPreset))
async def test_manual_resume_inherits_the_latest_managed_conversation_preset(rig, preset):
    from remote_agents.domain.state_machine import LifecycleEvent

    original = await rig.service.resume(
        ResumeCommand(
            PROJECT,
            CODEX,
            conversation(),
            "original-resume",
            permissions=preset,
        )
    )
    await rig.store.record_event(
        original.record.session_id, LifecycleEvent.RECONCILED_TERMINAL_MISSING
    )
    await rig.settings.set(
        PermissionPreset.ASK if preset != PermissionPreset.ASK else PermissionPreset.FULL_ACCESS
    )
    resumed = await rig.service.resume(
        ResumeCommand(PROJECT, CODEX, conversation(), "resume-again")
    )
    assert await rig.bindings.for_session(resumed.record.session_id) == preset


async def test_fresh_conversation_association_survives_mark_cleanup(rig, tmp_path):
    from remote_agents.adapters.agents.turn_markers import FileConversationMarks
    from remote_agents.domain.state_machine import LifecycleEvent

    marks = FileConversationMarks(tmp_path / "activity")
    rig.permissions._marks = marks
    original = await rig.service.launch(
        LaunchCommand(
            PROJECT,
            CODEX,
            "fresh-with-mark",
            permissions=PermissionPreset.ASK,
        )
    )
    marks.record(str(original.record.session_id), "source-123")
    await rig.store.record_event(
        original.record.session_id, LifecycleEvent.RECONCILED_TERMINAL_MISSING
    )
    await rig.service.remember_permissions()
    marks.forget(str(original.record.session_id))
    await rig.settings.set(PermissionPreset.FULL_ACCESS)
    resumed = await rig.service.resume(
        ResumeCommand(PROJECT, CODEX, conversation(), "resume-marked")
    )
    assert await rig.bindings.for_session(resumed.record.session_id) == PermissionPreset.ASK


async def test_legacy_session_keeps_unknown_history_and_uses_the_next_process_default(rig):
    from remote_agents.domain.models import SessionDisplayIdentity, SessionRecord, SessionState

    legacy = SessionRecord(
        SessionId.new(),
        PROJECT,
        CODEX,
        SessionDisplayIdentity(str(PROJECT), str(CODEX), "regular", 1),
        SessionState.ENDED,
        datetime.now(UTC),
    )
    await rig.store.save(legacy)
    assert await rig.service.permission_preset(legacy.session_id) is None
    await rig.settings.set(PermissionPreset.ASK)
    next_process = await rig.service.launch(
        LaunchCommand(
            PROJECT,
            CODEX,
            "legacy-continuation",
            inherit_permissions_of=legacy.session_id,
        )
    )
    assert await rig.bindings.for_session(next_process.record.session_id) == PermissionPreset.ASK
    assert await rig.bindings.for_session(legacy.session_id) is None


@pytest.mark.parametrize("preset", list(PermissionPreset))
async def test_a_scheduled_fire_resolves_the_current_default_once(rig, preset):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from remote_agents.adapters.sqlite.schedule_store import SQLiteScheduleStore
    from remote_agents.application.schedules import FireOutcome, SchedulePass
    from remote_agents.ports.schedules import Once, Schedule

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    schedule = Schedule(
        "preset-fire",
        PROJECT,
        CODEX,
        "fixture prompt",
        Once(now.replace(tzinfo=None)),
        False,
        now,
        now - timedelta(days=1),
    )
    schedules = SQLiteScheduleStore(rig.connection)
    await schedules.add(schedule)
    await rig.settings.set(preset)

    async def no(*args):
        return False

    async def notified(report):
        pass

    fire = SchedulePass(
        schedules,
        launch=rig.service.launch,
        send=rig.terminal.send_prompt,
        limit_stopped=no,
        working=no,
        notify=notified,
        zone=lambda: ZoneInfo("UTC"),
        now=lambda: now,
    )
    reports = await fire.run()
    assert len(reports) == 1 and reports[0].outcome == FireOutcome.STARTED
    await fire.run()
    assert rig.terminal.presets == [preset]


@pytest.mark.parametrize("preset", list(PermissionPreset))
async def test_the_restart_pass_inherits_the_snapshot_before_resuming_once(rig, preset):
    from remote_agents.adapters.sqlite.interrupted_resumes import SQLiteInterruptedResumes
    from remote_agents.application.interrupted_resume import (
        InterruptedResumePass,
        RestartSnapshot,
        SessionAtRestart,
    )
    from remote_agents.domain.state_machine import LifecycleEvent

    original = await rig.service.launch(
        LaunchCommand(
            PROJECT,
            CODEX,
            "before-restart",
            permissions=preset,
        )
    )
    snapshot = RestartSnapshot(
        sessions=(SessionAtRestart(original.record, pane_listed=False),),
        server_started_at=None,
        server_known=True,
        turn_marker_sessions=frozenset(),
        conversation_marks={str(original.record.session_id): "source-123"},
        rollovers=(),
        resumable_profiles=frozenset({CODEX}),
        pending=frozenset(),
    )
    await rig.store.record_event(
        original.record.session_id, LifecycleEvent.RECONCILED_TERMINAL_MISSING
    )
    await rig.settings.set(
        PermissionPreset.FULL_ACCESS
        if preset != PermissionPreset.FULL_ACCESS
        else PermissionPreset.ASK
    )

    async def folder(_session):
        return None

    restart = InterruptedResumePass(
        SQLiteInterruptedResumes(rig.connection),
        resume=rig.service.resume,
        folder_of=folder,
        key_claimed=rig.store.idempotency_key_claimed,
        send=rig.terminal.send_prompt,
        now=lambda: datetime.now(UTC),
    )
    reports = await restart.run(snapshot)
    assert len(reports) == 1
    assert reports[0].new_session_id is not None
    await restart.run(snapshot)
    assert rig.terminal.presets == [preset, preset]


@pytest.mark.parametrize("preset", list(PermissionPreset))
async def test_the_rollover_pass_inherits_the_snapshot_and_launches_one_successor(
    rig, preset, tmp_path
):
    from datetime import timedelta

    from remote_agents.adapters.agents.registry import adopt_templates
    from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
    from remote_agents.application.rollover import RolloverPass
    from remote_agents.ports.handoff_envelopes import HandoffEnvelope, HandoffEvent

    original = await rig.service.launch(
        LaunchCommand(
            PROJECT,
            CODEX,
            "predecessor",
            permissions=preset,
        )
    )
    await rig.settings.set(
        PermissionPreset.FULL_ACCESS
        if preset != PermissionPreset.FULL_ACCESS
        else PermissionPreset.ASK
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    envelope = HandoffEnvelope(
        HandoffEvent.READY,
        "h-0123456789abcdef0123",
        str(original.record.session_id),
        now,
        str(tmp_path / "plan.md"),
        None,
    )

    class Envelopes:
        def events(self, root):
            return (envelope,)

        def requested(self, root):
            return None

        def discard(self, *args):
            pass

        def clear_request(self, *args):
            pass

    async def yes(*args):
        return True

    rollover = RolloverPass(
        SQLiteRolloverStore(rig.connection),
        Envelopes(),
        adopt_templates={CODEX: adopt_templates()[CODEX]},
        sessions=rig.service.list_sessions,
        handoff_root=lambda project: tmp_path,
        enabled=yes,
        idle=yes,
        launch=rig.service.launch,
        send=rig.terminal.send_prompt,
        graceful_stop=rig.service.graceful_stop,
        notify=None,
        now=lambda: now,
    )
    await rollover.run_once()
    await rollover.run_once()
    assert rig.terminal.presets == [preset, preset]


@pytest.mark.parametrize("preset", list(PermissionPreset))
@pytest.mark.parametrize("resume", [False, True])
async def test_real_composition_writes_the_selected_argv_to_the_terminal_intent(
    rig, tmp_path, preset, resume
):
    import json
    from types import SimpleNamespace

    from remote_agents.adapters.tmux.gateway import TmuxInventory
    from remote_agents.adapters.tmux.runtime import TmuxTerminal
    from remote_agents.composition.backend import compose_backend
    from remote_agents.composition.tui import _profile_factory, _resume_profile_factory
    from remote_agents.config import AppConfig
    from remote_agents.production import ProductionPaths

    paths = ProductionPaths.for_home(tmp_path / "home")
    paths.ensure_directories()
    config = AppConfig(
        tmp_path,
        tmp_path / "registry.yaml",
        tmp_path / "sessions.sqlite3",
        40,
        10,
        30,
        path=rig.path,
    )

    class Gateway:
        intent_directory = tmp_path / "intents"

        async def inventory(self):
            return TmuxInventory((), ())

        async def capture(self, *args, **kwargs):
            return ""

        async def launch(self, session_id, project_id, profile_id, cwd):
            assert await rig.bindings.for_session(session_id) == preset

    terminal = TmuxTerminal(
        Gateway(),
        {PROJECT: tmp_path},
        {},
        startup_timeout=0.01,
        profile_factories={CODEX: _profile_factory(DEFINITION, Path("/fixture/codex"), {})},
        resume_profile_factories={
            CODEX: _resume_profile_factory(DEFINITION, Path("/fixture/codex"), {})
        },
    )
    projects = SimpleNamespace(
        paths={PROJECT: tmp_path},
        refresh=lambda: SimpleNamespace(catalogue=()),
    )
    backend = compose_backend(
        config,
        rig.connection,
        paths,
        projects=projects,
        runtime=SimpleNamespace(terminal=terminal, compatibility=()),
    )
    assert set(backend.permission_presets.profiles) == {CODEX}
    await backend.permission_presets.set(preset)
    command = (
        ResumeCommand(PROJECT, CODEX, conversation(), "composed-resume")
        if resume
        else LaunchCommand(PROJECT, CODEX, "composed-launch")
    )
    result = await (
        backend.sessions.resume(command) if resume else backend.sessions.launch(command)
    )
    intent = json.loads((Gateway.intent_directory / f"{result.record.session_id}.json").read_text())
    assert intent["argv"] == [
        "/fixture/codex",
        *(["resume", "source-123"] if resume else []),
        *EXPECTED[preset],
    ]


async def test_upgrade_preserves_legacy_rows_without_fabricating_snapshots(tmp_path):
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS, current_version
    from remote_agents.domain.models import SessionDisplayIdentity, SessionRecord, SessionState

    path = tmp_path / "upgrade.sqlite3"
    old = open_database(path, migrations=tuple(m for m in MIGRATIONS if m[0] <= 21))
    assert current_version(old) == 21
    record = SessionRecord(
        SessionId.new(),
        PROJECT,
        CODEX,
        SessionDisplayIdentity(str(PROJECT), str(CODEX), "regular", 1),
        SessionState.ENDED,
        datetime.now(UTC),
    )
    old.execute(
        "INSERT INTO sessions(session_id,project_id,profile_id,display_identity,state,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            str(record.session_id),
            str(PROJECT),
            str(CODEX),
            record.display.rendered,
            record.state.value,
            record.created_at.isoformat(),
        ),
    )
    old.commit()
    old.close()
    upgraded = open_database(path)
    try:
        assert current_version(upgraded) == MIGRATIONS[-1][0]
        assert await SQLiteSessionStore(upgraded).get(record.session_id) == record
        assert await SQLitePermissionBindings(upgraded).for_session(record.session_id) is None
    finally:
        upgraded.close()


async def test_invalid_default_refuses_a_new_launch_and_preserves_existing_panes(rig):
    from remote_agents.config import ConfigError

    original = await rig.service.launch(
        LaunchCommand(
            PROJECT,
            CODEX,
            "existing",
            permissions=PermissionPreset.ASK,
        )
    )
    rig.path.write_text('[codex]\ndefault_permissions = "invented"\n')
    with pytest.raises(ConfigError, match="permission preset"):
        await rig.service.launch(LaunchCommand(PROJECT, CODEX, "invalid-default"))
    assert len(await rig.store.list()) == 1
    assert (await rig.terminal.inspect(original.record.session_id)).live
    assert rig.terminal.presets == [PermissionPreset.ASK]


async def test_missing_inheritance_source_refuses_without_a_new_pane(rig):
    with pytest.raises(ValueError, match="permission source"):
        await rig.service.launch(
            LaunchCommand(
                PROJECT,
                CODEX,
                "missing-source",
                inherit_permissions_of=SessionId.new(),
            )
        )
    assert await rig.store.list() == ()
    assert rig.terminal.argv == []


async def test_snapshot_is_immutable_when_the_session_is_renamed(rig):
    import sqlite3

    original = await rig.service.launch(
        LaunchCommand(
            PROJECT,
            CODEX,
            "renamed",
            permissions=PermissionPreset.ASK,
        )
    )
    await rig.service.rename(original.record.session_id, "new label")
    assert await rig.bindings.for_session(original.record.session_id) == PermissionPreset.ASK
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        rig.connection.execute("UPDATE session_permissions SET preset = ?", ("full_access",))
