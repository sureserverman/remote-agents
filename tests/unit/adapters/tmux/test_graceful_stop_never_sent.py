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

import pytest

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


# --- A stop is never sent into a dialog (BL-055) -----------------------------------------------
#
# Every profile's stop sequence ends in `Enter` (`/exit Enter`, `/quit Enter Enter`), and every
# measured approval dialog opens on its yes option: a stop sent into one approves it. So the stop
# is judged from a styled capture under the key lock, like the relay's paste: a dialog or a draft
# refuses, and anything else -- idle, a running turn, a screen it cannot place -- still takes it,
# because a stop has to reach an agent mid-turn.


def _claude_pane(name: str):
    from .test_send_prompt import PromptPane

    fixtures = Path(__file__).resolve().parents[3] / "fixtures/panes/claude"
    screen = (fixtures / f"{name}.txt").read_text(encoding="utf-8")
    return PromptPane([screen])


def _composed_terminal(pane) -> TmuxTerminal:
    from remote_agents.adapters.agents.registry import profile_composers

    profile = LaunchProfile(
        "/usr/bin/claude", ("/usr/bin/claude",), {}, None, graceful_keys=("/exit", "Enter")
    )
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-graceful", pane),
        {},
        {_PROFILE: profile},
        startup_timeout=0.05,
        composers=profile_composers(),
    )


def test_a_graceful_stop_is_never_sent_into_a_dialog() -> None:
    import asyncio

    from remote_agents.ports.terminal import AGENT_ASKING

    pane = _claude_pane("dialog_approval")

    observation = asyncio.run(_composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE))

    assert observation.detail == AGENT_ASKING
    assert observation.live and not observation.preserved
    assert pane.keys == [], "the stop's Enter would have approved the dialog"


def test_a_graceful_stop_still_reaches_a_busy_pane() -> None:
    import asyncio

    pane = _claude_pane("busy")

    asyncio.run(_composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE))

    assert pane.keys == ["/exit", "Enter"]


def test_a_graceful_stop_judges_a_styled_capture_taken_under_the_lock() -> None:
    import asyncio

    pane = _claude_pane("idle_suggestion")

    asyncio.run(_composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE))

    assert pane.keys == ["/exit", "Enter"], "a dim suggestion is no draft"
    first_key = next(i for i, call in enumerate(pane.calls) if "send-keys" in call)
    captures = [call for call in pane.calls[:first_key] if "capture-pane" in call]
    assert captures and all("-e" in call for call in captures)


def test_a_stop_refused_at_a_dialog_is_recorded_as_never_sent() -> None:
    from remote_agents.application.services import _STOP_EVENTS
    from remote_agents.domain.state_machine import LifecycleEvent
    from remote_agents.ports.terminal import AGENT_ASKING

    assert _STOP_EVENTS[AGENT_ASKING] is LifecycleEvent.GRACEFUL_STOP_NEVER_SENT


def test_a_dialog_raised_partway_through_the_stop_gets_no_further_key() -> None:
    import asyncio

    from remote_agents.ports.terminal import AGENT_ASKING

    from .test_send_prompt import PromptPane

    fixtures = Path(__file__).resolve().parents[3] / "fixtures/panes/claude"
    pane = PromptPane(
        [(fixtures / "busy.txt").read_text(), (fixtures / "dialog_approval.txt").read_text()]
    )

    observation = asyncio.run(_composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE))

    assert pane.keys == ["/exit"], "the Enter would have approved the dialog that came up"
    assert observation.detail == AGENT_ASKING and observation.live


def test_a_stop_is_not_typed_into_shell_mode() -> None:
    """`! <cmd>` + `/exit` + `Enter` runs `<cmd>/exit` as a shell command, outside approvals."""
    import asyncio

    from remote_agents.ports.terminal import COMPOSER_HOLDS_TEXT

    pane = _claude_pane("composed_shell_mode")

    observation = asyncio.run(_composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE))

    assert observation.detail == COMPOSER_HOLDS_TEXT
    assert pane.keys == []


def test_a_stop_that_submits_nothing_still_goes_to_a_dialog() -> None:
    """OpenCode's `C-c` has no `Enter` to approve anything with, so no screen holds it back."""
    import asyncio

    from remote_agents.adapters.agents.registry import profile_composers

    from .test_send_prompt import PromptPane

    fixtures = Path(__file__).resolve().parents[3] / "fixtures/panes/opencode"
    dialog = next(fixtures.glob("dialog_*.txt")).read_text(encoding="utf-8")
    pane = PromptPane([dialog], profile="opencode")
    terminal = TmuxTerminal(
        TmuxGateway("remote-agents-test-graceful", pane),
        {},
        {
            ProfileId("opencode"): LaunchProfile(
                "/usr/bin/opencode", ("/usr/bin/opencode",), {}, None, graceful_keys=("C-c",)
            )
        },
        startup_timeout=0.05,
        composers=profile_composers(),
    )

    asyncio.run(terminal.graceful_stop(pane.session_id, ProfileId("opencode")))

    assert pane.keys == ["C-c"]


def test_a_codex_stop_sends_every_key_over_the_screens_between_them() -> None:
    """Between `/exit Enter Enter` Codex shows its command menu, then "Shutting down…".

    Captured on 0.155.1 (`fixtures/panes/stop_sequence/`). Both read UNKNOWN, not DIALOG, so the
    re-check before each later key lets the stop through; a dialog pattern loosened to match
    either would hold back every Codex stop, and this is what would say so.
    """
    import asyncio

    from remote_agents.adapters.agents.registry import profile_composers

    from .test_send_prompt import PromptPane

    panes = Path(__file__).resolve().parents[3] / "fixtures/panes"
    screens = [
        (panes / "codex" / "idle.txt").read_text(encoding="utf-8"),
        (panes / "stop_sequence" / "codex_after_exit_typed.txt").read_text(encoding="utf-8"),
        (panes / "stop_sequence" / "codex_after_first_enter.txt").read_text(encoding="utf-8"),
    ]
    pane = PromptPane(screens, profile="codex")
    keys = ("/exit", "Enter", "Enter")
    terminal = TmuxTerminal(
        TmuxGateway("remote-agents-test-graceful", pane),
        {},
        {
            ProfileId("codex"): LaunchProfile(
                "/usr/bin/codex", ("/usr/bin/codex",), {}, None, graceful_keys=keys
            )
        },
        startup_timeout=0.05,
        composers=profile_composers(),
    )

    asyncio.run(terminal.graceful_stop(pane.session_id, ProfileId("codex")))

    assert pane.keys == list(keys)


# --- cursor-agent, measured 2026-09-24 (`fixtures/panes/stop_sequence/`): 2026.09.18-9a7762b, ---
# --- and cursor_after_first_enter.txt on 2026.09.23-86fc751, the build the live drill runs on ---


def _cursor_terminal(pane) -> TmuxTerminal:
    from remote_agents.adapters.agents.registry import profile_composers
    from remote_agents.domain.profiles import closed_profiles

    keys = next(p.graceful_keys for p in closed_profiles() if str(p.profile_id) == "cursor-agent")
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-graceful", pane),
        {},
        {
            ProfileId("cursor-agent"): LaunchProfile(
                "/usr/bin/cursor-agent", ("/usr/bin/cursor-agent",), {}, None, graceful_keys=keys
            )
        },
        startup_timeout=0.05,
        composers=profile_composers(),
    )


def _panes(*names: str) -> list[str]:
    root = Path(__file__).resolve().parents[3] / "fixtures/panes"
    return [(root / name).read_text(encoding="utf-8") for name in names]


def test_a_cursor_stop_sends_every_key_over_its_command_menu() -> None:
    """After `/quit` cursor draws its command menu under the composer, which hides the status
    line; with the answered trust box still drawn above, the screen read as a trust dialog and
    the check before the next `Enter` refused every such stop (0.48.0). Only a dialog *pattern*
    on screen stops a sequence partway; a trust dialog is never raised mid-stop."""
    import asyncio

    from .test_send_prompt import PromptPane

    pane = PromptPane(
        _panes(
            "cursor/idle.txt",
            "stop_sequence/cursor_after_quit_typed.txt",
            "stop_sequence/cursor_after_first_enter.txt",
        ),
        profile="cursor-agent",
    )

    asyncio.run(_cursor_terminal(pane).graceful_stop(pane.session_id, ProfileId("cursor-agent")))

    assert pane.keys == ["/quit", "Enter", "Enter"]


@pytest.mark.parametrize("screen", ["cursor_shell_mode_empty", "cursor_shell_mode_command"])
def test_a_cursor_stop_is_not_typed_into_shell_mode(screen: str) -> None:
    """cursor-agent has a `!` shell mode too: `! Run a command — e.g., git status`."""
    import asyncio

    from remote_agents.ports.terminal import COMPOSER_HOLDS_TEXT

    from .test_send_prompt import PromptPane

    pane = PromptPane(_panes(f"stop_sequence/{screen}.txt"), profile="cursor-agent")

    observation = asyncio.run(
        _cursor_terminal(pane).graceful_stop(pane.session_id, ProfileId("cursor-agent"))
    )

    assert observation.detail == COMPOSER_HOLDS_TEXT
    assert pane.keys == []


def test_a_cursor_stop_gets_no_further_key_once_a_real_approval_comes_up() -> None:
    """`Run this command?` arriving after `/quit`: a declared dialog pattern, so the rest waits."""
    import asyncio

    from remote_agents.ports.terminal import AGENT_ASKING

    from .test_send_prompt import PromptPane

    pane = PromptPane(
        _panes("cursor/idle.txt", "cursor/dialog_approval.txt"), profile="cursor-agent"
    )

    observation = asyncio.run(
        _cursor_terminal(pane).graceful_stop(pane.session_id, ProfileId("cursor-agent"))
    )

    assert pane.keys == ["/quit"]
    assert observation.detail == AGENT_ASKING


def test_a_stop_is_never_sent_into_the_open_remote_control_menu() -> None:
    """`/exit Enter` into Claude's Remote Control menu selects its resting Continue: the menu
    closes and the stop reports `graceful_timeout` over an agent still running. The real menu
    (`fixtures/panes/remote_control/claude_menu.txt`, footer stored broken) refuses the stop."""
    import asyncio

    from remote_agents.ports.terminal import AGENT_ASKING

    from .test_send_prompt import PromptPane

    menu = _panes("remote_control/claude_menu.txt")[0].replace(
        "Esc to {continue}", "Esc to continue"
    )
    pane = PromptPane([menu])

    observation = asyncio.run(_composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE))

    assert observation.detail == AGENT_ASKING
    assert pane.keys == []
