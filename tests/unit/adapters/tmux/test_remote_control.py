"""The curated Claude Remote Control sequences, their classification, and the bare read."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.remote_control import (
    REMOTE_CONTROL_DISCONNECT_KEYS,
    REMOTE_CONTROL_ENABLE_KEYS,
    RemoteControlState,
    classify_remote_control_capture,
)
from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState as DomainRemoteControlState

# Two enums spell these three words, and a test that mixes them fails on `is` while printing
# two values that look identical. `classify_remote_control_capture` above answers in the tmux
# adapter's own `RemoteControlState`; `TmuxTerminal` converts to the domain's before it
# returns (`runtime._remote_control_state`), because the domain's is what the port, the
# record and both surfaces carry. The read below is a port method, so it is asserted against
# the domain's.


def test_remote_control_enable_and_disconnect_interactions_are_fixed() -> None:
    assert REMOTE_CONTROL_ENABLE_KEYS == ("/remote-control", "Enter")
    assert REMOTE_CONTROL_DISCONNECT_KEYS == ("Up", "Up", "Enter")


def test_capture_classification_uses_the_latest_known_transition() -> None:
    capture = "/remote-control is active\nRemote Control disconnected.\n"

    assert classify_remote_control_capture(capture) is RemoteControlState.INACTIVE


def test_capture_classification_fails_closed_for_unknown_output() -> None:
    assert classify_remote_control_capture("Claude Code") is RemoteControlState.UNKNOWN


# --- Reading the pane without typing at it ------------------------------------------------
#
# `remote_control` has always captured and classified before it acts -- that read is what
# lets it refuse a disable from an unreadable pane. Now that one button has to name the
# direction it implies, the *confirm* step needs the same reading with none of the
# consequences, so the terminal exposes the read on its own. The one property worth pinning
# mechanically is that it stays a read: a method that types into somebody's pane while the
# owner is still deciding whether to press the button is the failure this separation exists
# to make impossible.

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_PROFILE = ProfileId("claude")


def _pane(*, dead: str = "0", profile: str = "claude") -> str:
    return "|".join(
        (
            f"ra-{_SESSION}",
            "$1",
            "%3",
            "100",
            dead,
            "",
            "2",
            str(_SESSION),
            "opaque-editor",
            profile,
        )
    )


class _Runner:
    """Answers `list-panes` from a fixed listing and `capture-pane` from a fixed screen."""

    def __init__(self, listing: str, capture: str) -> None:
        self._listing = listing
        self._capture = capture
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if "list-panes" in argv:
            return self._listing
        if "capture-pane" in argv:
            return self._capture
        return ""

    @property
    def keys_sent(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if "send-keys" in call]


def _terminal(runner: _Runner) -> TmuxTerminal:
    profile = LaunchProfile(
        executable="/bin/sh",
        argv=("/bin/sh", "-c", "true"),
        environment={},
        readiness_marker=None,
        graceful_keys=("C-c",),
    )
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-remote-control-read", runner),
        {ProjectId("opaque-editor"): Path("/")},
        {_PROFILE: profile},
        startup_timeout=0.05,
    )


@pytest.mark.parametrize(
    ("capture", "expected"),
    [
        ("/remote-control is active\n", DomainRemoteControlState.ACTIVE),
        ("Disconnect this session\n", DomainRemoteControlState.ACTIVE),
        (
            "/remote-control is active\nRemote Control disconnected.\n",
            DomainRemoteControlState.INACTIVE,
        ),
        ("Claude Code\n", DomainRemoteControlState.UNKNOWN),
    ],
)
async def test_the_read_classifies_the_same_three_markers_the_toggle_does(
    capture: str, expected: DomainRemoteControlState
) -> None:
    runner = _Runner(_pane(), capture)

    assert await _terminal(runner).remote_control_state(_SESSION) is expected


async def test_the_read_types_nothing_at_the_pane() -> None:
    """The whole point of the separation: a reading costs the owner no keypress."""
    runner = _Runner(_pane(), "/remote-control is active\n")

    await _terminal(runner).remote_control_state(_SESSION)

    assert runner.keys_sent == [], "a read must never send keys into somebody's session"


@pytest.mark.parametrize(
    ("dead", "profile"),
    [("1", "claude"), ("0", "codex")],
)
async def test_a_pane_that_cannot_be_read_answers_unknown_rather_than_guessing(
    dead: str, profile: str
) -> None:
    """A dead pane and a non-Claude pane are both "no reading", which is what UNKNOWN means.

    Deliberately the same guard `remote_control` applies before it types, so the confirm
    screen and the mutation cannot disagree about whether this session is toggleable at all.
    """
    runner = _Runner(_pane(dead=dead, profile=profile), "/remote-control is active\n")

    assert (
        await _terminal(runner).remote_control_state(_SESSION) is DomainRemoteControlState.UNKNOWN
    )
    assert runner.keys_sent == []
