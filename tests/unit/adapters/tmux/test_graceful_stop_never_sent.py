"""A stop that could not have been delivered says so, rather than claiming an exit.

DEC-022 exists because the two outcomes are indistinguishable from the record otherwise:
an agent that exited because we asked it to, and an agent that was already gone when we
asked. tmux makes them easy to confuse — `send-keys` at a dead pane exits 0 and does
nothing (Claim 10) — so a stop that types first and looks afterwards finds `preserved`
already true and reports a graceful exit it did not cause.

The history that writes is the reason this matters: GRACEFUL_STOP_REQUESTED, then
PANE_EXITED, then CLEANUP_CONFIRMED — a durable claim that a sequence left this host and an
agent answered it. Nothing did. `unknown_session` is what routes to
`GRACEFUL_STOP_NEVER_SENT` instead (`application/services.py`).

Reachable with no console and no swap: a pane dies out of band — an OOM kill, a crash —
between one reconciliation pass and the owner pressing Stop.
"""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.application.session_actions import UNKNOWN_SESSION
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_PROFILE = ProfileId("claude")


def pane(*, dead: str) -> str:
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
            "claude",
        )
    )


class Runner:
    def __init__(self, listing: str, *, missing_on_keys: bool = False) -> None:
        self._listing = listing
        self._missing_on_keys = missing_on_keys
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if "list-panes" in argv:
            return self._listing
        if "send-keys" in argv and self._missing_on_keys:
            raise RuntimeError("tmux command failed: can't find pane: %3")
        return ""

    @property
    def keys_sent(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if "send-keys" in call]


def terminal(runner: Runner) -> TmuxTerminal:
    profile = LaunchProfile(
        executable="/bin/sh",
        argv=("/bin/sh", "-c", "true"),
        environment={},
        readiness_marker=None,
        graceful_keys=("C-c",),
    )
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-graceful", runner),
        {ProjectId("opaque-editor"): Path("/")},
        {_PROFILE: profile},
        startup_timeout=0.05,
    )


async def test_a_stop_into_an_already_dead_pane_is_never_sent_not_a_graceful_exit() -> None:
    runner = Runner(pane(dead="1"))

    observation = await terminal(runner).graceful_stop(_SESSION, _PROFILE)

    assert observation.detail == UNKNOWN_SESSION
    assert not observation.preserved, (
        "reporting preserved here would record PANE_EXITED for a pane that was already dead"
    )
    assert runner.keys_sent == [], "nothing should be typed at a pane that cannot receive it"


async def test_a_pane_vanishing_mid_sequence_is_reported_rather_than_raised() -> None:
    """The typed error used to escape the use case entirely, after the request event was
    already written, leaving the record at STOP_REQUESTED behind a generic "stop failed".
    An event that names its cause is what DEC-022 asks for, even an understated one."""
    runner = Runner(pane(dead="0"), missing_on_keys=True)

    observation = await terminal(runner).graceful_stop(_SESSION, _PROFILE)

    assert observation.detail == UNKNOWN_SESSION
    assert not observation.preserved


async def test_a_live_pane_still_gets_its_sequence() -> None:
    """The check must not become a refusal to stop anything: a live pane is typed at."""
    runner = Runner(pane(dead="0"))

    await terminal(runner).graceful_stop(_SESSION, _PROFILE)

    assert [call[-1] for call in runner.keys_sent] == ["C-c"]
