"""The capabilities this plan set out to give the local surface, each exercised by name.

The plan's opening paragraph enumerates exactly what the bot had and the terminal did not:
sessions list, detail and state, copy attach, graceful/cleanup/force stop, Claude Remote
Control, inspect output, and resume. Each gets its own probe below, so "parity" is a
checkable claim rather than a summary sentence, and losing one names itself in the failure.

Every probe drives the real app through Pilot and asserts an observable effect — a rendered
screen, or a command that reached the launcher. None of them can pass by falling through.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.widgets import OptionList

from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_REFERENCE = "c-" + "0" * 16


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


class _Everything:
    def __init__(self, state: SessionState) -> None:
        self.record = _record(state)
        self.issued: list[object] = []

    async def refresh_readiness(self):
        return (self.record,)

    async def list_sessions(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return "tmux -L remote-agents attach-session -t ra-x:"

    async def graceful_stop(self, command):
        self.issued.append(command)
        return None

    async def cleanup(self, command) -> None:
        self.issued.append(command)

    async def force_stop(self, command):
        self.issued.append(command)
        return None

    async def set_remote_control(self, command):
        self.issued.append(command)
        return RemoteControlState.ACTIVE

    async def resume(self, command):
        self.issued.append(command)
        return self.record


class _Conversations:
    def _summary(self, reference: ConversationReference) -> ConversationSummary:
        return ConversationSummary(
            reference,
            ProfileId("claude"),
            ProjectId("opaque-existing"),
            ConversationState.RESUMABLE,
            datetime.now(UTC),
            description="a saved conversation",
        )

    async def catalogue(self, query):
        return ConversationCataloguePage(
            (self._summary(ConversationReference(_REFERENCE)),), query.page, 1
        )

    async def resolve_for_resume(self, reference):
        return ResolvedConversation(self._summary(reference), ProviderConversationId("pid"))

    async def capabilities(self):
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)


async def _capture(_session_id) -> str:
    return "Claude Code ready"


def _app(state: SessionState = SessionState.RUNNING) -> tuple[RemoteAgentsTui, _Everything]:
    launcher = _Everything(state)
    return (
        RemoteAgentsTui(
            TuiContext(
                launcher=launcher,  # type: ignore[arg-type]
                creator=object(),  # type: ignore[arg-type]
                profiles=(ProfileChoice("claude", True),),
                refresh_catalogue=lambda: (_PROJECT,),
                attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
                catalogue=(_PROJECT,),
                capture=_capture,
                conversations=_Conversations(),  # type: ignore[arg-type]
            )
        ),
        launcher,
    )


def _choices(app: RemoteAgentsTui) -> OptionList:
    return app.screen.query_one("#choices", OptionList)


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in _choices(app).options]


def _status(app: RemoteAgentsTui) -> str:
    return str(app.screen.query_one("#status").content)


def _keys(app: RemoteAgentsTui) -> list[str | None]:
    return [option.id for option in _choices(app).options]


async def _choose(app, pilot, key: str) -> None:
    """Select a rendered entry by key, refusing to act on one the surface never offered.

    Calling the handler directly would prove only that a method exists. A capability the
    owner cannot reach is not a capability, so every probe goes through this.

    The event is built from the widget's own option rather than from a stand-in: `OptionList`
    carries row identity as `Option.id`, so the message this constructs is the one the widget
    would post, and a key the surface never rendered cannot be smuggled past the assertion by
    fabricating an item to carry it.
    """
    assert key in _keys(app), f"the surface offers no {key!r} entry to select"
    choices = _choices(app)
    index = choices.get_option_index(key)
    await app.screen.on_option_list_option_selected(
        OptionList.OptionSelected(choices, choices.get_option_at_index(index), index)
    )
    await pilot.pause()


async def _open_detail(app, launcher, pilot) -> None:
    """Reach the detail the way an owner does: through the sessions list."""
    await pilot.press("ctrl+s")
    await pilot.pause()
    await _choose(app, pilot, str(launcher.record.session_id))


# --- one probe per capability; each asserts an observable effect --------------------


async def _probe_sessions_list(app, launcher, pilot) -> None:
    await pilot.press("ctrl+s")
    await pilot.pause()
    assert app._step is Step.SESSIONS
    assert any("existing" in row for row in _rows(app))


async def _probe_detail(app, launcher, pilot) -> None:
    await app.action_sessions()
    await pilot.pause()
    await _choose(app, pilot, str(launcher.record.session_id))
    assert app._step is Step.SESSION_DETAIL
    assert launcher.record.display.rendered in _status(app)
    assert launcher.record.state.value in _status(app)


async def _probe_copy_attach(app, launcher, pilot) -> None:
    await _open_detail(app, launcher, pilot)
    await _choose(app, pilot, "attach")
    assert "attach-session" in _status(app)


async def _probe_graceful(app, launcher, pilot) -> None:
    await _open_detail(app, launcher, pilot)
    await _choose(app, pilot, "graceful")
    assert any(isinstance(item, GracefulStopCommand) for item in launcher.issued)


async def _probe_cleanup(app, launcher, pilot) -> None:
    await _open_detail(app, launcher, pilot)
    await _choose(app, pilot, "cleanup")
    assert any(isinstance(item, CleanupCommand) for item in launcher.issued)


async def _probe_force(app, launcher, pilot) -> None:
    await _open_detail(app, launcher, pilot)
    await _choose(app, pilot, "force")
    assert app._step is Step.FORCE_CONFIRM
    assert launcher.issued == [], "force must not fire on the first selection"
    await _choose(app, pilot, "force-confirm")
    assert any(isinstance(item, ForceStopCommand) for item in launcher.issued)


async def _probe_remote_control(app, launcher, pilot) -> None:
    await _open_detail(app, launcher, pilot)
    await _choose(app, pilot, "remote-control")
    assert app._step is Step.REMOTE_CONTROL_CONFIRM
    await _choose(app, pilot, "remote-control-active")
    assert any(isinstance(item, RemoteControlCommand) for item in launcher.issued)


async def _probe_inspect(app, launcher, pilot) -> None:
    await _open_detail(app, launcher, pilot)
    await _choose(app, pilot, "inspect")
    assert app._step is Step.INSPECT
    assert "Claude Code ready" in str(app.screen.query_one("#output").content)


async def _probe_resume(app, launcher, pilot) -> None:
    await pilot.press("ctrl+o")
    await pilot.pause()
    await _choose(app, pilot, "opaque-existing")
    await _choose(app, pilot, "claude")
    await _choose(app, pilot, _REFERENCE)
    assert app._step is Step.RESUME_CONFIRM
    assert launcher.issued == [], "resume must not fire on the selection alone"
    await _choose(app, pilot, "resume-confirm")
    assert any(isinstance(item, ResumeCommand) for item in launcher.issued)
    assert app.return_value is not None, "a ready resume must hand back an attach request"


# The state each capability needs to be offered at all, from the shared policy.
CAPABILITIES = {
    "sessions list": (_probe_sessions_list, SessionState.RUNNING),
    "session detail and state": (_probe_detail, SessionState.RUNNING),
    "copy attach": (_probe_copy_attach, SessionState.RUNNING),
    "graceful stop": (_probe_graceful, SessionState.RUNNING),
    "cleanup": (_probe_cleanup, SessionState.PRESERVED),
    "force stop": (_probe_force, SessionState.RUNNING),
    "claude remote control": (_probe_remote_control, SessionState.RUNNING),
    "inspect output": (_probe_inspect, SessionState.RUNNING),
    "resume": (_probe_resume, SessionState.RUNNING),
}


@pytest.mark.parametrize("capability", sorted(CAPABILITIES))
async def test_the_local_surface_has_every_bot_capability(capability: str) -> None:
    probe, state = CAPABILITIES[capability]
    app, launcher = _app(state)
    async with app.run_test() as pilot:
        await probe(app, launcher, pilot)


def test_every_capability_reached_by_a_binding_has_one() -> None:
    """Two capabilities are entered by keystroke rather than by a rendered row.

    Without this, deleting a binding makes the capability unreachable while every probe
    that called the action method directly stayed green — which is the same "a method
    exists" mistake the probes were rewritten to avoid.
    """
    bound = {binding.key: binding.action for binding in RemoteAgentsTui.BINDINGS}
    assert bound.get("ctrl+s") == "sessions"
    assert bound.get("ctrl+o") == "resume"


def test_the_parity_claim_names_every_capability_the_plan_enumerated() -> None:
    """Guards the set itself: shrinking it would make the parametrized test weaker silently."""
    expected = {
        "sessions list",
        "session detail and state",
        "copy attach",
        "graceful stop",
        "cleanup",
        "force stop",
        "claude remote control",
        "inspect output",
        "resume",
    }
    assert set(CAPABILITIES) == expected
