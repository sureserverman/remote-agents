"""The console's Permissions action: a running Codex session's mode, changed from its detail.

One row and one pick (DEC-033), the bot's twin: the detail lists Permissions, which opens the
three live choices (`LIVE_CHOICES`, OD-3) with the current one highlighted. A pick runs the
change from the screen's handler, on the app's pump, and announces the shared outcome sentence
(DEC-091). No global key is added for it (DEC-095, DEC-052).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from backends import SessionUseCaseDouble, backend_for
from textual.widgets import OptionList
from tui_feedback import announcements
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.permission_presets import (
    LIVE_CHOICES,
    PermissionChange,
    PermissionSettings,
    permission_change_sentence,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_bindings import PermissionState

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(profile: str = "codex", state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId(profile),
        SessionDisplayIdentity("existing", profile, "regular", 1),
        state,
        datetime.now(UTC),
    )


class _Setting:
    async def read(self) -> PermissionPreset:
        return PermissionPreset.APPROVE_FOR_ME

    async def write(self, preset: PermissionPreset) -> None:
        raise AssertionError("a live change never writes the Settings default")


@dataclass(slots=True)
class _Sessions(SessionUseCaseDouble):
    records: tuple[SessionRecord, ...] = ()
    requested: PermissionPreset = PermissionPreset.ASK
    outcome: PermissionChange | None = None
    changes: list[tuple[SessionId, PermissionPreset]] = field(default_factory=list)
    refusal: str | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def permission_preset(self, _session_id) -> PermissionPreset:
        return PermissionPreset.ASK

    async def permission_state(self, _session_id) -> PermissionState:
        return PermissionState(self.requested, self.requested, "d", None)

    async def permission_change_refusal(self, _session_id) -> str | None:
        return self.refusal

    async def change_session_permissions(self, session_id, preset) -> PermissionChange:
        self.changes.append((session_id, preset))
        outcome = self.outcome or PermissionChange(True, preset)
        if outcome.changed:
            self.requested = preset
        return outcome


def _app(sessions: _Sessions) -> RemoteAgentsTui:
    backend = replace(
        backend_for(
            sessions=sessions,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_EXISTING,),
            catalogue=(_EXISTING,),
        ),
        permission_presets=PermissionSettings(
            _Setting(), profiles={ProfileId("codex"): tuple(PermissionPreset)}
        ),
    )
    return RemoteAgentsTui(
        TuiContext(
            backend=backend,
            profiles=(ProfileAvailability("codex", True), ProfileAvailability("claude", True)),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [option.id for option in app.screen.query_one("#choices", OptionList).options]


async def _open_picker(app: RemoteAgentsTui, pilot, record: SessionRecord) -> None:
    await app.show_detail(str(record.session_id))
    await pilot.pause()
    await app.screen.choose("permissions")
    await pilot.pause()


async def test_a_running_codex_session_lists_permissions() -> None:
    record = _record()
    app = _app(_Sessions((record,)))
    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        rows = _rows(app)
    assert "Permissions" in rows


async def test_a_claude_or_not_running_session_lists_no_permissions() -> None:
    for record in (_record("claude"), _record(state=SessionState.STARTING)):
        app = _app(_Sessions((record,)))
        async with app.run_test() as pilot:
            await app.show_detail(str(record.session_id))
            await pilot.pause()
            rows = _rows(app)
        assert "Permissions" not in rows, record


async def test_the_screens_choices_are_exactly_the_live_choices() -> None:
    record = _record()
    app = _app(_Sessions((record,)))
    async with app.run_test() as pilot:
        await _open_picker(app, pilot, record)
        keys = [key for key in _keys(app) if key in {p.value for p in PermissionPreset}]
        rows = _rows(app)
    assert keys == [choice.value.value for choice in LIVE_CHOICES]
    assert not any("Use Codex configuration" in row for row in rows)
    assert not any("default" in row.casefold() for row in rows)


async def test_the_picker_highlights_the_current_mode() -> None:
    record = _record()
    app = _app(_Sessions((record,), requested=PermissionPreset.FULL_ACCESS))
    async with app.run_test() as pilot:
        await _open_picker(app, pilot, record)
        choices = app.screen.query_one("#choices", OptionList)
        highlighted = choices.get_option_at_index(choices.highlighted).id
    assert highlighted == PermissionPreset.FULL_ACCESS.value


async def test_a_pick_changes_once_and_announces_the_shared_sentence() -> None:
    record = _record()
    sessions = _Sessions((record,))
    app = _app(sessions)
    async with app.run_test() as pilot:
        await _open_picker(app, pilot, record)
        await app.screen.choose(PermissionPreset.FULL_ACCESS.value)
        await pilot.pause()
        said = announcements(app)
        step = position(app)
    assert sessions.changes == [(record.session_id, PermissionPreset.FULL_ACCESS)]
    assert permission_change_sentence(PermissionChange(True, PermissionPreset.FULL_ACCESS)) in said
    assert step == "SESSION_DETAIL"


async def test_a_failed_pick_announces_not_changed_with_its_reason() -> None:
    record = _record()
    outcome = PermissionChange(False, PermissionPreset.FULL_ACCESS, "no Codex daemon")
    app = _app(_Sessions((record,), outcome=outcome))
    async with app.run_test() as pilot:
        await _open_picker(app, pilot, record)
        await app.screen.choose(PermissionPreset.FULL_ACCESS.value)
        await pilot.pause()
        warned = announcements(app, severity="warning")
    assert "not changed: no Codex daemon" in warned


async def test_no_global_key_is_added_for_it() -> None:
    bindings = [
        binding if isinstance(binding, tuple) else (binding.key, binding.action)
        for binding in RemoteAgentsTui.BINDINGS
    ]
    assert not any("permission" in str(action).casefold() for _key, action, *_ in bindings)


async def test_a_session_that_can_never_change_is_refused_without_a_picker() -> None:
    """The close-out demo: a "Use Codex configuration" session opened three modes and refused
    only after a pick. Now the row says why at once, and no picker opens."""
    record = _record()
    reason = "uses Codex's own configuration; relaunch with a mode to change it live"
    sessions = _Sessions((record,), refusal=reason)
    app = _app(sessions)
    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("permissions")
        await pilot.pause()
        warned = announcements(app, severity="warning")
        still_detail = "Permissions" in _rows(app)
    assert f"cannot change permissions: {reason}" in warned
    assert still_detail, "no picker opened over the detail"
    assert sessions.changes == []
