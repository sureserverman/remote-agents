"""Permission choices use one closed catalogue and one persisted setting."""

from pathlib import Path

import pytest

from remote_agents.application.permission_presets import (
    PERMISSION_CHOICES,
    PermissionSettings,
    permission_label,
)
from remote_agents.composition.limits_source import ConfigPermissionSetting
from remote_agents.config import ConfigError, read_codex_permissions
from remote_agents.domain.profiles import PermissionPreset


def setting_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("# keep this comment\n[limits]\nproject_page_size = 10\n")
    return path


@pytest.mark.parametrize("preset", list(PermissionPreset))
async def test_each_choice_persists_and_is_reread_by_another_process(tmp_path, preset):
    path = setting_file(tmp_path)
    first = PermissionSettings(ConfigPermissionSetting(path))
    other = PermissionSettings(ConfigPermissionSetting(path))
    await first.set(preset)
    assert await other.read() == preset
    assert isinstance(await other.read(), PermissionPreset)
    assert read_codex_permissions(path) == preset
    assert "# keep this comment\n[limits]\nproject_page_size = 10\n" in path.read_text()


async def test_an_absent_key_uses_the_approved_default(tmp_path):
    settings = PermissionSettings(ConfigPermissionSetting(setting_file(tmp_path)))
    assert await settings.read() == PermissionPreset.APPROVE_FOR_ME
    assert {choice.value for choice in PERMISSION_CHOICES} == set(PermissionPreset)
    assert [choice.label for choice in PERMISSION_CHOICES] == [
        "Approve for me",
        "Ask me",
        "Full access",
        "Use Codex configuration",
    ]


@pytest.mark.parametrize("bad", ["invented", "", "--dangerously-bypass-hook-trust", True, None])
async def test_invalid_choices_refuse_without_changing_the_file(tmp_path, bad):
    path = setting_file(tmp_path)
    original = path.read_bytes()
    settings = PermissionSettings(ConfigPermissionSetting(path))
    with pytest.raises(ValueError, match="permission preset"):
        await settings.set(bad)
    assert path.read_bytes() == original


async def test_a_failed_save_is_reported_and_does_not_change_the_previous_choice(
    tmp_path, monkeypatch
):
    from remote_agents.composition import limits_source

    path = setting_file(tmp_path)
    settings = PermissionSettings(ConfigPermissionSetting(path))

    def refuse(*args):
        raise ConfigError("cannot write configuration: read-only file")

    monkeypatch.setattr(limits_source, "write_config_key", refuse)
    with pytest.raises(ConfigError, match="cannot write"):
        await settings.set(PermissionPreset.ASK)
    assert await settings.read() == PermissionPreset.APPROVE_FOR_ME


@pytest.mark.parametrize(
    "text", ['[codex]\ndefault_permissions = "invented"', "[codex", "codex = false"]
)
async def test_an_invalid_persisted_value_is_a_refusal_not_an_implicit_default(tmp_path, text):
    path = setting_file(tmp_path)
    path.write_text(text)
    with pytest.raises(ConfigError):
        await PermissionSettings(ConfigPermissionSetting(path)).read()


def test_the_string_schema_and_typed_choices_have_the_same_closed_values():
    from remote_agents.config import CODEX_PERMISSION_PRESETS

    assert set(CODEX_PERMISSION_PRESETS) == {preset.value for preset in PermissionPreset}


# --- Resume follows the current default (Task 3.1, supersedes part of DEC-133) ---------------


def _resume_rig(tmp_path: Path):
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
    from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
    from remote_agents.adapters.tmux.fake import FakeTerminal
    from remote_agents.application.permission_presets import SessionPermissions
    from remote_agents.application.services import SessionService
    from remote_agents.domain.models import ProfileId

    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteSessionStore(connection)
    bindings = SQLitePermissionBindings(connection)
    path = tmp_path / "config.toml"
    path.write_text('[codex]\ndefault_permissions = "approve_for_me"\n')
    settings = PermissionSettings(
        ConfigPermissionSetting(path), profiles={ProfileId("codex"): tuple(PermissionPreset)}
    )
    service = SessionService(
        store, FakeTerminal(), permissions=SessionPermissions(settings, bindings, store)
    )
    return connection, store, bindings, settings, service


def _conversation(source: str = "source-123"):
    from datetime import UTC, datetime

    from remote_agents.domain.conversations import (
        ConversationReference,
        ConversationState,
        ConversationSummary,
        ProviderConversationId,
        ResolvedConversation,
    )
    from remote_agents.domain.models import ProfileId, ProjectId

    return ResolvedConversation(
        ConversationSummary(
            ConversationReference("c-" + "a" * 20),
            ProfileId("codex"),
            ProjectId("fixture-project"),
            ConversationState.RESUMABLE,
            datetime.now(UTC),
        ),
        ProviderConversationId(source),
    )


@pytest.mark.parametrize("default", [PermissionPreset.APPROVE_FOR_ME, PermissionPreset.ASK])
async def test_resume_without_a_pick_uses_the_current_default_not_the_conversations_old_preset(
    tmp_path, default
):
    from remote_agents.application.commands import ResumeCommand
    from remote_agents.domain.models import ProfileId, ProjectId
    from remote_agents.domain.state_machine import LifecycleEvent

    connection, store, bindings, settings, service = _resume_rig(tmp_path)
    try:
        codex, project = ProfileId("codex"), ProjectId("fixture-project")
        earlier = await service.resume(
            ResumeCommand(
                project, codex, _conversation(), "earlier", permissions=PermissionPreset.FULL_ACCESS
            )
        )
        await store.record_event(
            earlier.record.session_id, LifecycleEvent.RECONCILED_TERMINAL_MISSING
        )
        await settings.set(default)
        again = await service.resume(ResumeCommand(project, codex, _conversation(), "again"))
        assert await bindings.for_session(again.record.session_id) == default
        picked = await service.resume(
            ResumeCommand(
                project,
                codex,
                _conversation("source-456"),
                "picked",
                permissions=PermissionPreset.FULL_ACCESS,
            )
        )
        assert await bindings.for_session(picked.record.session_id) == PermissionPreset.FULL_ACCESS
    finally:
        connection.close()


async def test_a_rollover_from_a_full_access_predecessor_still_inherits_full_access(tmp_path):
    from remote_agents.application.commands import LaunchCommand
    from remote_agents.domain.models import ProfileId, ProjectId

    connection, _store, bindings, settings, service = _resume_rig(tmp_path)
    try:
        codex, project = ProfileId("codex"), ProjectId("fixture-project")
        predecessor = await service.launch(
            LaunchCommand(project, codex, "pred", permissions=PermissionPreset.FULL_ACCESS)
        )
        await settings.set(PermissionPreset.ASK)
        successor = await service.launch(
            LaunchCommand(
                project, codex, "succ", inherit_permissions_of=predecessor.record.session_id
            )
        )
        assert (
            await bindings.for_session(successor.record.session_id) == PermissionPreset.FULL_ACCESS
        )
    finally:
        connection.close()


def test_the_old_conversation_association_is_left_in_place_with_no_destructive_migration(
    tmp_path,
):
    from remote_agents.adapters.sqlite import migrations
    from remote_agents.adapters.sqlite.database import open_database

    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(session_permissions)")}
        assert "conversation_id" in columns
    finally:
        connection.close()
    text = Path(migrations.__file__).read_text()
    assert "DROP COLUMN conversation_id" not in text
    assert "DROP TABLE session_permissions" not in text


def test_the_no_pick_choice_names_the_current_default_and_no_saved_snapshot():
    from remote_agents.application.permission_presets import permission_label

    assert permission_label(None) == "Current default"


# --- Live change (Task 3.2, OD-3; supersedes part of DEC-133) ----------------------------------


class _ChangeRig:
    """A running Codex session over the real store, and a stand-in for `serve`'s apply pass:
    each wait the change takes runs `pass_step` once, as one pass would."""

    def __init__(self, tmp_path: Path, *, profile: str = "codex", state: str = "running"):
        from datetime import UTC, datetime

        from remote_agents.adapters.sqlite.database import open_database
        from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
        from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
        from remote_agents.application.permission_presets import SessionPermissions
        from remote_agents.domain.models import (
            ProfileId,
            ProjectId,
            SessionDisplayIdentity,
            SessionId,
            SessionRecord,
            SessionState,
        )

        self.connection = open_database(tmp_path / "sessions.sqlite3")
        self.store = SQLiteSessionStore(self.connection)
        self.bindings = SQLitePermissionBindings(self.connection)
        path = tmp_path / "config.toml"
        path.write_text('[codex]\ndefault_permissions = "approve_for_me"\n')
        self.settings = PermissionSettings(
            ConfigPermissionSetting(path), profiles={ProfileId("codex"): tuple(PermissionPreset)}
        )
        self.clock = 0.0
        self.waits = 0
        self.pass_step = None
        self.session = SessionId.new()
        self.record = SessionRecord(
            self.session,
            ProjectId("fixture-project"),
            ProfileId(profile),
            SessionDisplayIdentity("fixture-project", profile, "regular", 1),
            SessionState(state),
            datetime.now(UTC),
        )

        async def sleep(seconds: float) -> None:
            self.waits += 1
            self.clock += seconds
            if self.pass_step is not None:
                await self.pass_step(self)

        self.permissions = SessionPermissions(
            self.settings, self.bindings, self.store, sleep=sleep, clock=lambda: self.clock
        )

    async def launch(self, preset=PermissionPreset.ASK, *, legacy: bool = False) -> None:
        from remote_agents.domain.models import ProfileId

        if legacy or self.record.profile_id != ProfileId("codex"):
            # Launched by v0.69.0, or by a provider with no presets: no state row is written.
            await self.store.save(self.record)
            if legacy:
                self.connection.execute(
                    "INSERT INTO session_permissions(session_id, preset) VALUES (?, ?)",
                    (str(self.session), preset.value),
                )
                self.connection.commit()
            return
        await self.store.save(self.record, permissions=preset)
        await self.bindings.mark_applied(self.session, preset, "daemon-1")


async def _applies(rig) -> None:
    state = await rig.bindings.state(rig.session)
    await rig.bindings.mark_applied(rig.session, state.requested, "daemon-1")


async def _fails(rig) -> None:
    state = await rig.bindings.state(rig.session)
    await rig.bindings.mark_failed(rig.session, "no Codex daemon", requested=state.requested)


@pytest.fixture
def change_rig(tmp_path):
    rig = _ChangeRig(tmp_path)
    yield rig
    rig.connection.close()


def test_change_offers_exactly_the_three_explicit_modes():
    from remote_agents.application.permission_presets import LIVE_CHOICES

    assert tuple(choice.value for choice in LIVE_CHOICES) == (
        PermissionPreset.APPROVE_FOR_ME,
        PermissionPreset.ASK,
        PermissionPreset.FULL_ACCESS,
    )
    assert {choice.label for choice in LIVE_CHOICES} <= {c.label for c in PERMISSION_CHOICES}


@pytest.mark.parametrize(
    "preset",
    [PermissionPreset.APPROVE_FOR_ME, PermissionPreset.FULL_ACCESS],
)
async def test_change_sets_the_request_and_reports_the_pass_outcome(change_rig, preset):
    from remote_agents.application.permission_presets import permission_change_sentence

    await change_rig.launch(PermissionPreset.ASK)
    change_rig.pass_step = _applies
    outcome = await change_rig.permissions.change(change_rig.session, preset)
    assert outcome.changed and outcome.preset is preset
    assert permission_change_sentence(outcome) == f"now {permission_label(preset)}"
    state = await change_rig.bindings.state(change_rig.session)
    assert (state.requested, state.applied, state.failure) == (preset, preset, None)
    # The launch snapshot is history and does not move.
    assert await change_rig.bindings.for_session(change_rig.session) is PermissionPreset.ASK


async def test_change_refuses_the_provider_default_by_name(change_rig):
    await change_rig.launch(PermissionPreset.ASK)
    outcome = await change_rig.permissions.change(
        change_rig.session, PermissionPreset.PROVIDER_DEFAULT
    )
    assert not outcome.changed
    assert "Use Codex configuration" in outcome.reason
    assert (await change_rig.bindings.state(change_rig.session)).requested is PermissionPreset.ASK
    assert change_rig.waits == 0


async def test_change_refuses_a_session_of_a_provider_with_no_presets(tmp_path):
    rig = _ChangeRig(tmp_path, profile="claude")
    try:
        await rig.launch()
        outcome = await rig.permissions.change(rig.session, PermissionPreset.ASK)
        assert not outcome.changed
        assert "claude" in outcome.reason
    finally:
        rig.connection.close()


@pytest.mark.parametrize("state", ["ended", "stop_requested", "starting", "untrusted"])
async def test_change_refuses_a_session_that_is_not_running_by_name(tmp_path, state):
    rig = _ChangeRig(tmp_path, state=state)
    try:
        await rig.launch()
        outcome = await rig.permissions.change(rig.session, PermissionPreset.FULL_ACCESS)
        assert not outcome.changed
        assert "ended" in outcome.reason or "not running" in outcome.reason
        assert (await rig.bindings.state(rig.session)).requested is PermissionPreset.ASK
    finally:
        rig.connection.close()


async def test_change_refuses_a_session_launched_with_flags_by_name(change_rig):
    from remote_agents.application.permission_presets import permission_change_sentence

    await change_rig.launch(PermissionPreset.FULL_ACCESS, legacy=True)
    outcome = await change_rig.permissions.change(change_rig.session, PermissionPreset.ASK)
    assert not outcome.changed
    assert permission_change_sentence(outcome) == (
        "not changed: launched with flags; relaunch to change permissions"
    )
    assert await change_rig.bindings.state(change_rig.session) is None


async def test_a_failed_change_restores_the_earlier_mode_and_returns_the_reason(change_rig):
    from remote_agents.application.permission_presets import permission_change_sentence

    await change_rig.launch(PermissionPreset.ASK)
    change_rig.pass_step = _fails
    outcome = await change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    assert not outcome.changed
    assert permission_change_sentence(outcome) == (
        "not changed: no Codex daemon; Ask me is being set again"
    )
    state = await change_rig.bindings.state(change_rig.session)
    assert state.requested is PermissionPreset.ASK
    # What the server holds is unknown after a failed apply, so the pass applies the earlier
    # mode again rather than trusting a confirmation that may no longer hold.
    assert state.applied is None and state.failure is None


async def test_a_change_with_no_outcome_in_ten_seconds_restores_the_earlier_mode(change_rig):
    await change_rig.launch(PermissionPreset.ASK)
    outcome = await change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    assert not outcome.changed
    assert outcome.reason
    assert change_rig.clock <= 10.0
    state = await change_rig.bindings.state(change_rig.session)
    assert (state.requested, state.applied) == (PermissionPreset.ASK, None)


async def test_a_late_failure_after_a_restore_is_not_recorded_against_the_earlier_mode(change_rig):
    await change_rig.launch(PermissionPreset.ASK)
    await change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    # The pass's apply of the abandoned mode reports late: it is not about what is asked now.
    assert not await change_rig.bindings.mark_failed(
        change_rig.session, "timed out", requested=PermissionPreset.FULL_ACCESS
    )
    assert (await change_rig.bindings.state(change_rig.session)).failure is None


async def test_a_rollover_after_a_change_inherits_the_changed_preset(change_rig):
    await change_rig.launch(PermissionPreset.ASK)
    change_rig.pass_step = _applies
    assert (
        await change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    ).changed
    await change_rig.settings.set(PermissionPreset.APPROVE_FOR_ME)
    inherited = await change_rig.permissions.resolve(
        change_rig.record.profile_id, None, change_rig.session
    )
    assert inherited is PermissionPreset.FULL_ACCESS


async def test_the_backend_change_reaches_the_session_use_case(change_rig):
    from remote_agents.adapters.tmux.fake import FakeTerminal
    from remote_agents.application.backend import Backend
    from remote_agents.application.services import SessionService

    await change_rig.launch(PermissionPreset.ASK)
    change_rig.pass_step = _applies
    backend = Backend(
        sessions=SessionService(
            change_rig.store, FakeTerminal(), permissions=change_rig.permissions
        )
    )
    outcome = await backend.change_session_permissions(
        change_rig.session, PermissionPreset.APPROVE_FOR_ME
    )
    assert outcome.changed
    assert await Backend().change_session_permissions(
        change_rig.session, PermissionPreset.ASK
    ) == outcome.__class__(False, PermissionPreset.ASK, "permission presets are unavailable")


async def test_change_refuses_while_another_change_is_being_applied(change_rig):
    """Two surfaces at once: the second must not make the first's unconfirmed request the
    preset its own failure restores (Tier-1 review, Critical)."""
    await change_rig.launch(PermissionPreset.ASK)
    await change_rig.bindings.set_requested(change_rig.session, PermissionPreset.FULL_ACCESS)
    outcome = await change_rig.permissions.change(
        change_rig.session, PermissionPreset.APPROVE_FOR_ME
    )
    assert not outcome.changed and "still being applied" in outcome.reason
    assert change_rig.waits == 0
    state = await change_rig.bindings.state(change_rig.session)
    assert (state.requested, state.applied) == (PermissionPreset.FULL_ACCESS, PermissionPreset.ASK)


async def test_a_failure_restores_what_was_confirmed_not_an_unconfirmed_request(change_rig):
    await change_rig.launch(PermissionPreset.ASK)
    change_rig.pass_step = _fails
    await change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    assert (await change_rig.bindings.state(change_rig.session)).requested is PermissionPreset.ASK


async def test_a_late_confirmation_of_an_abandoned_preset_is_not_recorded(change_rig):
    await change_rig.launch(PermissionPreset.ASK)
    await change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    assert not await change_rig.bindings.mark_applied(
        change_rig.session, PermissionPreset.FULL_ACCESS, "daemon-1"
    )
    state = await change_rig.bindings.state(change_rig.session)
    assert (state.requested, state.applied) == (PermissionPreset.ASK, None)


async def test_change_refuses_a_session_on_the_providers_own_configuration_by_name(change_rig):
    """OD-12: nothing on the server means "the provider's own configuration", so a failed
    change could not be undone there. Refused before anything is asked for."""
    await change_rig.launch(PermissionPreset.PROVIDER_DEFAULT)
    outcome = await change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    assert not outcome.changed
    assert "Codex's own configuration" in outcome.reason and "relaunch" in outcome.reason
    assert change_rig.waits == 0
    state = await change_rig.bindings.state(change_rig.session)
    assert state.requested is PermissionPreset.PROVIDER_DEFAULT


# --- Stage 3 gate, remediation round 1 --------------------------------------------------------


async def test_the_od12_refusal_says_the_owners_exact_sentence(change_rig):
    from remote_agents.application.permission_presets import permission_change_sentence

    await change_rig.launch(PermissionPreset.PROVIDER_DEFAULT)
    outcome = await change_rig.permissions.change(change_rig.session, PermissionPreset.ASK)
    assert permission_change_sentence(outcome) == (
        "not changed: uses Codex's own configuration; relaunch with a mode to change it live"
    )


async def test_a_successor_inherits_the_confirmed_mode_while_a_change_is_in_flight(change_rig):
    """Tier-2: a rollover during a change's wait must not hand its successor the unconfirmed
    pick, which the owner may be told a moment later was not made."""
    await change_rig.launch(PermissionPreset.ASK)
    await change_rig.bindings.set_requested(change_rig.session, PermissionPreset.FULL_ACCESS)
    inherited = await change_rig.permissions.resolve(
        change_rig.record.profile_id, None, change_rig.session
    )
    assert inherited is PermissionPreset.ASK


async def test_a_successor_after_a_failed_change_inherits_the_launch_mode(change_rig):
    await change_rig.launch(PermissionPreset.ASK)
    await change_rig.bindings.set_requested(change_rig.session, PermissionPreset.FULL_ACCESS)
    await change_rig.bindings.mark_failed(
        change_rig.session, "no Codex daemon", requested=PermissionPreset.FULL_ACCESS
    )
    inherited = await change_rig.permissions.resolve(
        change_rig.record.profile_id, None, change_rig.session
    )
    assert inherited is PermissionPreset.ASK


async def test_a_change_that_did_not_take_says_the_earlier_mode_is_set_again(change_rig):
    """Second pass: the apply may have landed late, so the sentence must not imply the pane is
    certainly on the earlier mode; it says the earlier one is being set again."""
    from remote_agents.application.permission_presets import permission_change_sentence

    await change_rig.launch(PermissionPreset.ASK)
    outcome = await change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    assert permission_change_sentence(outcome) == (
        "not changed: it was not applied in time; Ask me is being set again"
    )


async def test_a_cancelled_change_still_restores_the_earlier_mode(change_rig):
    import asyncio

    await change_rig.launch(PermissionPreset.ASK)
    started = asyncio.Event()

    async def hang(rig) -> None:
        started.set()
        await asyncio.Event().wait()

    change_rig.pass_step = hang
    task = asyncio.create_task(
        change_rig.permissions.change(change_rig.session, PermissionPreset.FULL_ACCESS)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = await change_rig.bindings.state(change_rig.session)
    assert (state.requested, state.applied) == (PermissionPreset.ASK, None)


async def test_picking_the_current_mode_applies_it_afresh_before_saying_now(change_rig):
    """Second pass: after a server restart the stored confirmation is stale, so "now <mode>"
    is said only once the pass has applied it again."""
    await change_rig.launch(PermissionPreset.ASK)
    seen = []

    async def applies_after_look(rig) -> None:
        seen.append((await rig.bindings.state(rig.session)).applied)
        await _applies(rig)

    change_rig.pass_step = applies_after_look
    outcome = await change_rig.permissions.change(change_rig.session, PermissionPreset.ASK)
    assert outcome.changed
    assert seen and seen[0] is None, "the old confirmation was not cleared before waiting"
