"""Console operations are named methods over codec-generated targets, like every other.

No free-text target reaches the runner, because no method takes one: each builds its own
argv from typed identifiers through the codec (DEC-001). This file used to open by saying
the generic `mutate` entry "still refuses everything it refused" — that entry is gone, its
guard having been circular (the only thing needing an allow-list was the entry point that
carried it), and the shape that replaced it is asserted in
`tests/architecture/test_the_agent_is_addressed_by_pane.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_EXACT = "ra-01234567-89ab-cdef-0123-456789abcdef:"
_BASE = ("tmux", "-L", "remote-agents-test-console")


class RecordingRunner:
    def __init__(self, output: str = "", error: RuntimeError | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        return self.output


def gateway(runner: RecordingRunner) -> TmuxGateway:
    return TmuxGateway("remote-agents-test-console", runner)


async def test_console_exists_asks_has_session_and_reads_an_absent_server_as_no() -> None:
    runner = RecordingRunner()
    assert await gateway(runner).console_exists() is True
    assert runner.calls == [(*_BASE, "has-session", "-t", "ra-console:")]

    absent = RecordingRunner(error=RuntimeError("no server running on /tmp/tmux-1000/x"))
    assert await gateway(absent).console_exists() is False

    gone = RecordingRunner(error=RuntimeError("can't find session: ra-console"))
    assert await gateway(gone).console_exists() is False


async def test_create_console_runs_the_dashboard_command_detached(tmp_path: Path) -> None:
    runner = RecordingRunner()
    await gateway(runner).create_console((sys.executable, "-m", "remote_agents"), tmp_path)
    assert runner.calls == [
        (
            *_BASE,
            "new-session",
            "-d",
            "-s",
            "ra-console",
            "-c",
            str(tmp_path),
            sys.executable,
            "-m",
            "remote_agents",
        )
    ]


async def test_create_console_refuses_an_empty_command_or_a_bad_directory(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    with pytest.raises(ValueError):
        await gateway(runner).create_console((), tmp_path)
    with pytest.raises(ValueError):
        await gateway(runner).create_console(("cmd",), Path("relative/dir"))
    assert runner.calls == []






async def test_the_status_flash_uses_a_generated_target_only() -> None:
    """What is left of this once the tab mechanism's focus and switch operations are gone.

    `select-window`, `switch-client -t <session>` and `switch-client -t ra-console:` were the
    console's three ways to move a client between windows and sessions. All three retired
    with the tabs (Task 2.4): the console has one window, and it reaches an agent by
    exchanging panes rather than by moving the client anywhere.
    """
    runner = RecordingRunner()
    await gateway(runner).display_message("agent finished: opaque-editor")
    assert runner.calls == [
        (*_BASE, "display-message", "-l", "--", "agent finished: opaque-editor"),
    ]


async def test_the_console_binding_is_installed_on_our_socket_with_a_validated_key() -> None:
    """Both actions of the key budget, on our socket, with the key validated either way.

    `select-window 0` was what *every* binding did under the tab model. It cannot be that
    now: under the swap model the owner is already on window 0, and what they need is the
    projects surface exchanged back into the left slot — which tmux cannot decide on its own,
    because it cannot read our pane marks. So the projects key runs our program instead.
    """
    from remote_agents.ports.console import ConsoleBindingAction

    runner = RecordingRunner()
    projects = ("/usr/bin/python3", "-m", "remote_agents", "console", "projects")
    await gateway(runner).install_console_binding(
        "F12", ConsoleBindingAction.SHOW_PROJECTS, projects
    )
    await gateway(runner).install_console_binding("C-a", ConsoleBindingAction.FOCUS_NEXT_PANE)
    assert runner.calls == [
        (
            *_BASE,
            "bind-key",
            "-n",
            "F12",
            "run-shell",
            "/usr/bin/python3 -m remote_agents console projects",
        ),
        (*_BASE, "bind-key", "-n", "C-a", "select-pane", "-t", ":.+"),
    ]
    for key in ("", "two words", "a;b", "$(rm)", "péché", "C-", "C-;"):
        with pytest.raises(ValueError):
            await gateway(runner).install_console_binding(
                key, ConsoleBindingAction.FOCUS_NEXT_PANE
            )


async def test_a_binding_whose_action_and_command_disagree_is_refused() -> None:
    """A projects key with nothing to run, or a focus key carrying a command, is a mistake
    that would otherwise install a binding that quietly does the wrong thing."""
    from remote_agents.ports.console import ConsoleBindingAction

    runner = RecordingRunner()
    with pytest.raises(ValueError, match="needs the command"):
        await gateway(runner).install_console_binding("F12", ConsoleBindingAction.SHOW_PROJECTS)
    with pytest.raises(ValueError, match="takes no command"):
        await gateway(runner).install_console_binding(
            "F11", ConsoleBindingAction.FOCUS_NEXT_PANE, ("anything",)
        )
    assert runner.calls == [], "a refused binding must not reach tmux at all"


async def test_a_command_with_a_space_in_its_path_stays_one_command() -> None:
    """`run-shell` takes one shell string, so an unquoted join is a different command.

    The composition root builds this from `sys.executable`, which on a real host is exactly
    the kind of path that turns out to have a space in it.
    """
    from remote_agents.ports.console import ConsoleBindingAction

    runner = RecordingRunner()
    await gateway(runner).install_console_binding(
        "F12",
        ConsoleBindingAction.SHOW_PROJECTS,
        ("/home/a b/python", "-m", "remote_agents", "console", "projects"),
    )
    assert runner.calls[0][-1] == "'/home/a b/python' -m remote_agents console projects"


async def test_a_command_bearing_a_hash_never_reaches_tmux_own_format_engine() -> None:
    """`/bin/sh` is not the only reader of a `run-shell` string — tmux expands it first.

    The mirror of the space-in-path test above, for the second interpreter. Probed on real
    tmux 3.4 rather than read off the manual: `run-shell "echo '#{pane_id}'"` printed `%0`,
    and `run-shell "echo '#(id -u)'"` printed nothing, because tmux ran the `#(...)` through
    its own format engine. `shlex.quote` leaves `#` alone — it is not a shell metacharacter
    there — so without doubling it, a path containing `#{` or `#(` would be substituted away
    or would execute. The same probe confirms `##` is the escape: `##{pane_id}` came back as
    the literal `#{pane_id}`.

    Not reachable from today's caller, which builds a fixed tuple from `sys.executable`. It
    is closed now because the next binding built from a project path or a profile name would
    reintroduce it silently.
    """
    from remote_agents.ports.console import ConsoleBindingAction

    runner = RecordingRunner()
    await gateway(runner).install_console_binding(
        "F12",
        ConsoleBindingAction.SHOW_PROJECTS,
        ("/opt/py#{pane_id}/bin/python", "-m", "remote_agents", "console", "projects"),
    )
    command = runner.calls[0][-1]
    assert "##{pane_id}" in command, "an unescaped # is a tmux format, not a path"
    assert "#{pane_id}" not in command.replace("##{pane_id}", ""), "no bare format survived"

    runner = RecordingRunner()
    await gateway(runner).install_console_binding(
        "F12", ConsoleBindingAction.SHOW_PROJECTS, ("/opt/#(id -u)/python", "-m", "remote_agents")
    )
    assert "##(id -u)" in runner.calls[0][-1], "#(...) runs a command through tmux's engine"


async def test_a_gone_target_is_typed_for_every_single_target_console_operation() -> None:
    """The race capture()/mutate() were built for — the object vanishing between the
    caller's decision and the call landing — gets the same TerminalTargetMissing typing
    on every new single-target operation, so existing handlers catch it uniformly."""
    from remote_agents.ports.console import ConsolePaneSlot
    from remote_agents.ports.terminal import TerminalTargetMissing

    gone = RuntimeError("can't find session: whatever")
    with pytest.raises(TerminalTargetMissing):
        await gateway(RecordingRunner(error=gone)).mark_console_slot(
            "%3", ConsolePaneSlot.FEED
        )
    with pytest.raises(TerminalTargetMissing):
        await gateway(RecordingRunner(error=gone)).split_console_pane(
            "%1", ("true",), Path("/tmp"), vertical=True, percent=33
        )


async def test_a_broken_tmux_is_never_misread_as_an_absent_console() -> None:
    """The unmatched-error branch: anything that is not an absent server or target keeps
    its type and propagates — the failure mode the inventory docstring warns about."""
    broken = RuntimeError("server exited unexpectedly")
    with pytest.raises(RuntimeError, match="server exited unexpectedly"):
        await gateway(RecordingRunner(error=broken)).console_exists()
    with pytest.raises(RuntimeError, match="server exited unexpectedly"):
        await gateway(RecordingRunner(error=broken)).console_zoomed_pane()


async def test_the_zoom_probe_parses_and_degrades_per_branch() -> None:
    """Every branch of the flash's guard, which reads a zoom flag now rather than a window.

    An unzoomed console, garbage, an absent console and an absent server all answer "nothing
    is hiding the feed" — the honest reading, and the one that leaves the flash silent rather
    than firing on a probe that failed. A genuinely broken tmux keeps its error type, so it
    is not misread as a console that simply is not zoomed.
    """
    assert await gateway(RecordingRunner(output="1|%4\n")).console_zoomed_pane() == "%4"
    assert await gateway(RecordingRunner(output="0|%4\n")).console_zoomed_pane() is None
    assert await gateway(RecordingRunner(output="garbage")).console_zoomed_pane() is None
    assert await gateway(RecordingRunner(output="1|notapane")).console_zoomed_pane() is None
    for message in ("no server running on /tmp/x", "can't find session: ra-console"):
        absent = RecordingRunner(error=RuntimeError(message))
        assert await gateway(absent).console_zoomed_pane() is None
    with pytest.raises(RuntimeError, match="server exited"):
        await gateway(
            RecordingRunner(error=RuntimeError("server exited unexpectedly"))
        ).console_zoomed_pane()


async def test_launch_stamps_identity_on_the_pane_and_leaves_the_session_bare(
    tmp_path: Path,
) -> None:
    """Every identity option `-p`, and not one of them at session scope.

    The absence is the load-bearing half. tmux resolves `#{@option}` by falling back
    pane → session, so a session-scoped twin keeps answering with the agent's identity
    after the agent's pane has moved out — and whatever pane takes its place inherits it.
    Probed on tmux 3.4 (2026-08-19) and pinned live in
    `tests/contract/adapters/tmux/test_feature_probe.py`.
    """
    runner = RecordingRunner()
    await gateway(runner).launch(_SESSION, ProjectId("opaque-editor"), ProfileId("claude"), tmp_path)

    identity = (
        "@remote_agents_schema",
        "@remote_agents_id",
        "@remote_agents_project_id",
        "@remote_agents_profile",
    )
    marks = [call for call in runner.calls if any(option in call for option in identity)]
    assert [call[3:] for call in marks] == [
        ("set-option", "-p", "-t", _EXACT, "@remote_agents_schema", "2"),
        ("set-option", "-p", "-t", _EXACT, "@remote_agents_id", str(_SESSION)),
        ("set-option", "-p", "-t", _EXACT, "@remote_agents_project_id", "opaque-editor"),
        ("set-option", "-p", "-t", _EXACT, "@remote_agents_profile", "claude"),
    ]
    assert all(call[:3] == _BASE for call in marks)
    assert not [call for call in marks if "-p" not in call]


async def test_launch_still_makes_the_pane_survive_its_agent(tmp_path: Path) -> None:
    """`remain-on-exit` is set on the **pane**, so the protection travels with the agent.

    It is a window option, and a window option does not move with a pane: armed at window
    scope, an agent swapped into another window lands unprotected, and its exit destroys the
    pane outright — no `pane_dead` evidence for DEC-021's read-only attach, and the host
    window losing its last pane takes that session with it. An earlier draft of this test
    asserted the session-scoped form and explained that identity could not be inherited from
    it, which was true and beside the point: the question is not what can be inherited but
    what travels. Pinned live as Claim 9."""
    runner = RecordingRunner()
    await gateway(runner).launch(_SESSION, ProjectId("opaque-editor"), ProfileId("claude"), tmp_path)

    assert (*_BASE, "set-option", "-p", "-t", _EXACT, "remain-on-exit", "on") in runner.calls
