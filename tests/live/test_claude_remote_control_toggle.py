"""Disposable live qualification of the one-button Claude Remote Control toggle.

Three readings and two presses against one real `claude` pane, on a socket this test
creates and destroys. The two presses are the *same* button: what decides the direction is
`TerminalPort.remote_control_state`, read between the press and the question, resolved by
`remote_control_target`. That pairing is the surface's whole behaviour with the surface
taken out, which is what makes it drillable here at all.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import profile_composers
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.application.session_actions import (
    remote_control_reading,
    remote_control_target,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.domain.remote_control import RemoteControlState


@pytest.mark.live_acceptance
async def test_claude_remote_control_toggle_on_an_exact_disposable_managed_pane(
    tmp_path: Path,
) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_PROFILE") != "claude":
        pytest.skip("BLOCKED: claude_profile_not_selected")
    project_value = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT")
    if project_value is None:
        pytest.skip("BLOCKED: trusted_project_not_configured")
    project_path = Path(project_value).resolve(strict=True)
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("BLOCKED: executable_missing")

    definition = next(
        profile for profile in closed_profiles() if profile.profile_id == ProfileId("claude")
    )
    session_id = SessionId.new()
    project_id = ProjectId("qualification")
    profile = build_launch_profile(
        definition,
        Path(executable),
        session_id,
        {
            key: os.environ[key]
            for key in ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
            if key in os.environ
        },
    )
    socket = f"remote-agents-test-{session_id.value.hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
    terminal = TmuxTerminal(
        gateway,
        {project_id: project_path},
        {definition.profile_id: profile},
        startup_timeout=20,
        # The toggle types only onto an idle composer (BL-055); without one it refuses.
        composers=profile_composers(),
    )
    try:
        launched = await terminal.launch(session_id, project_id, definition.profile_id)
        assert launched.live, launched.detail

        # --- Press one: a pane nobody has toggled ------------------------------------------
        #
        # This is the UNKNOWN case, taken from a real pane rather than contrived: a freshly
        # launched Claude shows none of the three markers, so the read that a confirmation
        # takes answers UNKNOWN, and the policy offers *on* only. The plan suggested reaching
        # UNKNOWN by scrolling the marker out of the capture; a pane that has never been
        # toggled is the same reading, is what production actually meets first, and is
        # deterministic here instead of depending on how tall the pane happens to be.
        #
        # And on 2.1.269 the pane is in fact **already connected** -- it connects itself at
        # launch, printing nothing. So this press is a no-op that discovers the truth: the
        # enable keys open the status menu instead of enabling, the terminal recognises the
        # menu, dismisses it with Escape, and reports ACTIVE.
        assert await terminal.remote_control_state(session_id) is RemoteControlState.UNKNOWN
        first = remote_control_target(RemoteControlState.UNKNOWN)
        assert first is RemoteControlState.ACTIVE, (
            "an unreadable pane must be offered on -- disabling opens Claude's status menu "
            "and arrowing through a menu we cannot see is what DEC-003 refuses"
        )
        enabled_state = await terminal.remote_control(session_id, first)
        assert enabled_state is RemoteControlState.ACTIVE

        # --- Press two: the same button, the other way -------------------------------------
        #
        # Nothing about the button changed between the two presses. What changed is what this
        # session is known to be -- and knowing it takes both sources, which is the finding
        # this drill exists to hold.
        #
        # The pane itself still says nothing. Remote Control being on is announced only inside
        # the status menu, and the press above dismissed that menu rather than leaving it over
        # the owner's work; no transition line was printed, because nothing transitioned. So
        # the bare read is UNKNOWN and `remote_control_reading` supplies what press one
        # observed, which is what the record carries in production.
        assert await terminal.remote_control_state(session_id) is RemoteControlState.UNKNOWN
        observed = remote_control_reading(RemoteControlState.UNKNOWN, enabled_state)
        assert observed is RemoteControlState.ACTIVE
        disabled_state = await terminal.remote_control(session_id, remote_control_target(observed))
        assert disabled_state is RemoteControlState.INACTIVE

        # --- And *this* transition does leave a line, so the bare read follows it back ------
        assert await terminal.remote_control_state(session_id) is RemoteControlState.INACTIVE
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass


class _Markers:
    """The turn markers Claude's hook writes in production, kept in memory for one pane."""

    def __init__(self) -> None:
        self._started: dict[str, object] = {}

    def start(self, session_id: str) -> None:
        from datetime import UTC, datetime

        self._started[session_id] = datetime.now(UTC)

    def end(self, session_id: str) -> None:
        self._started.pop(session_id, None)

    def started_at(self, session_id: str):
        return self._started.get(session_id)

    def sessions(self) -> tuple[str, ...]:
        return tuple(self._started)


class _Store:
    """Just enough of a session store to watch whether a refused press writes a reading."""

    def __init__(self, record) -> None:
        self.record = record
        self.writes: list[RemoteControlState] = []

    async def get(self, session_id):
        return self.record if session_id == self.record.session_id else None

    async def claim_idempotency_key(self, key):
        return True

    async def set_remote_control_state(self, session_id, state):
        self.writes.append(state)
        return self.record


@pytest.mark.live_acceptance
async def test_a_press_is_refused_mid_turn_with_its_reason(tmp_path: Path) -> None:
    """BL-109 on a real pane: a press while a turn runs is refused BUSY, types nothing, and
    leaves the stored reading. Spends one short Claude turn."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from remote_agents.application.commands import RemoteControlCommand
    from remote_agents.application.services import SessionService
    from remote_agents.domain.models import SessionDisplayIdentity, SessionRecord, SessionState
    from remote_agents.ports.terminal import (
        PromptOutcome,
        RemoteControlRefusal,
        RemoteControlRefused,
    )

    if os.environ.get("REMOTE_AGENTS_LIVE_PROFILE") != "claude":
        pytest.skip("BLOCKED: claude_profile_not_selected")
    project_value = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT")
    if project_value is None:
        pytest.skip("BLOCKED: trusted_project_not_configured")
    project_path = Path(project_value).resolve(strict=True)
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("BLOCKED: executable_missing")

    definition = next(
        profile for profile in closed_profiles() if profile.profile_id == ProfileId("claude")
    )
    session_id = SessionId.new()
    project_id = ProjectId("qualification")
    profile = build_launch_profile(
        definition,
        Path(executable),
        session_id,
        {
            key: os.environ[key]
            for key in ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
            if key in os.environ
        },
    )
    socket = f"remote-agents-test-{session_id.value.hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
    markers = _Markers()
    terminal = TmuxTerminal(
        gateway,
        {project_id: project_path},
        {definition.profile_id: profile},
        startup_timeout=20,
        composers=profile_composers(),
        turn_markers=markers,
    )
    record = SessionRecord(
        session_id,
        project_id,
        definition.profile_id,
        SessionDisplayIdentity("qualification", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    store = _Store(replace(record, remote_control_state=RemoteControlState.ACTIVE))
    service = SessionService(store, terminal)
    try:
        launched = await terminal.launch(session_id, project_id, definition.profile_id)
        assert launched.live, launched.detail

        # As Claude's `UserPromptSubmit` hook does in production: the turn is marked started.
        markers.start(str(session_id))
        sent = await terminal.send_prompt(
            session_id, "Write one paragraph of about 150 words on why terminals multiplex."
        )
        assert sent.outcome is PromptOutcome.SENT, sent
        before = await gateway.capture(session_id)

        with pytest.raises(RemoteControlRefused) as refused:
            await service.set_remote_control(
                RemoteControlCommand(session_id, RemoteControlState.INACTIVE, "live-mid-turn")
            )

        assert refused.value.reason is RemoteControlRefusal.BUSY
        assert store.writes == [], "a refused press wrote a reading over the stored ACTIVE"
        assert store.record.remote_control_state is RemoteControlState.ACTIVE
        after = await gateway.capture(session_id)
        assert "/remote-control" not in after or "/remote-control" in before, (
            "the press typed into a running turn"
        )
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass
