"""Bare `remote-agents` enters the console; everything with arguments stays the CLI.

The bare name was unclaimed — no arguments fell through every subcommand branch and
exited 0 silently — so claiming it breaks nothing that ever worked. The routing honors
the hosting the same way opening a session does: a bare shell ensures the console and
execs the attach to it; a client already on our server is told it is already there; a
foreign tmux client gets the command printed, never a nested client. `agent-event`'s
pre-bootstrap fast path in `__main__` is untouched — it matches on the literal first
argument, which an empty argv never has.
"""

from __future__ import annotations

import pytest

from remote_agents import bootstrap

_OURS = {"TMUX": "/tmp/tmux-1000/remote-agents,12345,0"}
_FOREIGN = {"TMUX": "/tmp/tmux-1000/default,999,1"}


async def _ensured() -> bool:
    return True


async def _not_ensured() -> bool:
    return False


def test_no_arguments_routes_to_the_console_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    entered: list[bool] = []
    monkeypatch.setattr(bootstrap, "_enter_console", lambda: entered.append(True) or 0)
    assert bootstrap.main([]) == 0
    assert entered == [True]


def test_a_subcommand_never_reaches_the_console_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        bootstrap, "_enter_console", lambda: pytest.fail("a subcommand must stay the CLI")
    )
    assert bootstrap.main(["doctor", "--profiles", "--json"]) == 0
    assert "profiles" in capsys.readouterr().out


def test_a_bare_shell_ensures_the_console_and_execs_the_attach() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    code = bootstrap._enter_console(
        environment={},
        ensure_console=_ensured,
        exec_argv=lambda program, argv: calls.append((program, argv)),
    )
    assert code == 0
    assert calls == [
        ("tmux", ("tmux", "-L", "remote-agents", "attach-session", "-t", "ra-console:"))
    ]


def test_inside_the_console_it_says_so_instead_of_nesting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = bootstrap._enter_console(
        environment=_OURS,
        ensure_console=_ensured,
        exec_argv=lambda p, a: pytest.fail("a client already on our server must not exec"),
    )
    assert code == 0
    # Both of the console's keys are named, because this line is the only place the running
    # program tells the owner what it binds — `doctor` answers whether a console is possible,
    # never what it costs in keys.
    printed = capsys.readouterr().out
    assert "Already in the console" in printed
    assert "F12" in printed and "prefix h" in printed, printed


def test_inside_foreign_tmux_the_command_is_printed_never_nested(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = bootstrap._enter_console(
        environment=_FOREIGN,
        ensure_console=_ensured,
        exec_argv=lambda p, a: pytest.fail("a foreign client must not exec"),
    )
    assert code == 0
    assert "attach-session -t ra-console:" in capsys.readouterr().out


def test_a_console_that_cannot_be_prepared_exits_nonzero_without_exec(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = bootstrap._enter_console(
        environment={},
        ensure_console=_not_ensured,
        exec_argv=lambda p, a: pytest.fail("an unprepared console must not be attached"),
    )
    assert code == 1
    assert "doctor" in capsys.readouterr().err


def test_an_exec_that_cannot_run_leaves_the_owner_the_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(program: str, argv: tuple[str, ...]) -> None:
        raise OSError("tmux is not on PATH")

    code = bootstrap._enter_console(environment={}, ensure_console=_ensured, exec_argv=refuse)
    assert code == 1
    assert "attach-session -t ra-console:" in capsys.readouterr().err


# --- `remote-agents pane <name>`: one surface per tmux pane (Sub-plan 3, Task 1.1) ---
#
# The console is four tmux panes and a Textual app cannot span panes, so the surface is four
# processes rather than one app with four widgets. It was three until the agent-limits pane
# joined the right-hand column between sessions and the feed. Routing is what this task owns:
# which name composes which surface, that an unknown name is refused before anything is
# composed, and that adding the verb moved nothing that already routed.


def test_each_pane_name_composes_its_own_surface() -> None:
    from remote_agents.adapters.tui.panes import PANE_SURFACES

    assert set(PANE_SURFACES) == {"projects", "sessions", "limits", "feed"}
    # Four names, four distinct surfaces — not one class answering to two keys, which would
    # route correctly and render the same pane twice.
    assert len(set(PANE_SURFACES.values())) == 4


def test_the_pane_command_routes_the_name_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(bootstrap, "_enter_pane", lambda name, config: seen.append(name) or 0)
    for name in ("projects", "sessions", "feed"):
        assert bootstrap.main(["pane", name]) == 0
    assert seen == ["projects", "sessions", "feed"]


def test_an_unknown_pane_name_is_refused_by_the_pane_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap, "_enter_pane", lambda name, config: pytest.fail("an unknown pane must not run")
    )
    with pytest.raises(SystemExit) as refusal:
        bootstrap.main(["pane", "nonsense"])
    assert refusal.value.code != 0


def test_a_missing_pane_name_is_refused_rather_than_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap, "_enter_pane", lambda name, config: pytest.fail("a nameless pane must not run")
    )
    with pytest.raises(SystemExit) as refusal:
        bootstrap.main(["pane"])
    assert refusal.value.code != 0


def test_the_pane_command_never_reaches_the_console_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap, "_enter_console", lambda: pytest.fail("a subcommand must stay the CLI")
    )
    monkeypatch.setattr(bootstrap, "_enter_pane", lambda name, config: 0)
    assert bootstrap.main(["pane", "feed"]) == 0


def test_tui_still_routes_beside_the_pane_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The combined dashboard stays: a bare terminal has one pane, not three.

    Driven through `main(["tui"])`, which is the claim. The first version of this test
    asserted `main(["doctor", "--profiles", "--json"])` — a duplicate of the subcommand test
    twelve lines up, in which the string `tui` never appeared. It could not have failed if
    `tui` had stopped routing, which a gate evaluator pointed out.
    """
    from remote_agents.adapters.tui.app import run_local_terminal

    routed: list[object] = []
    monkeypatch.setattr(
        bootstrap, "_enter_pane", lambda name, config: pytest.fail("tui is not a pane surface")
    )
    monkeypatch.setattr(
        bootstrap, "_enter_console", lambda: pytest.fail("a subcommand must stay the CLI")
    )
    monkeypatch.setattr(
        bootstrap, "_run_surface", lambda config, runner, label: routed.append(runner) or 0
    )
    assert bootstrap.main(["tui"]) == 0
    assert routed == [run_local_terminal], "tui runs the combined dashboard, not a pane"


def test_the_pane_command_and_tui_run_the_same_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One composition body, two surfaces — so the confinement, the migration, the lease and
    the attach handoff cannot drift between them. They were two hand-copied copies, and a
    Tier-2 review caught them already diverging inside one stage."""
    labels: list[str] = []
    monkeypatch.setattr(
        bootstrap, "_run_surface", lambda config, runner, label: labels.append(label) or 0
    )
    assert bootstrap.main(["tui"]) == 0
    assert bootstrap.main(["pane", "feed"]) == 0
    assert labels == ["the local terminal surface", "the feed pane"]


async def test_an_unknown_pane_name_is_refused_by_the_surface_too() -> None:
    """The argparse `choices` is the outer guard; the composition refuses on its own too.

    Both are real: `main` is not the only caller, and a surface that trusted its argument
    would compose a database and a catalogue before discovering there is nothing to run.
    """
    from remote_agents.adapters.tui.panes import run_pane_surface

    with pytest.raises(ValueError, match="unknown console pane"):
        run_pane_surface("nonsense", object())  # type: ignore[arg-type]


async def test_the_pane_runner_seam_receives_the_surface_the_name_selects() -> None:
    from remote_agents.adapters.tui.panes import FeedPane, SessionsPane, run_pane_surface

    seen: list[type] = []
    for name, expected in (("sessions", SessionsPane), ("feed", FeedPane)):
        run_pane_surface(name, object(), runner=lambda surface, context: seen.append(surface))  # type: ignore[arg-type]
        assert seen[-1] is expected


def test_the_composition_root_does_not_load_the_terminal_library() -> None:
    """`serve` must never import Textual, so a failure in it cannot reach the bot.

    That invariant became load-bearing when `PANE_NAMES` moved into `adapters.tui` so the
    argument parser could name the panes without importing what a pane *is*. Nothing enforced
    it; a gate evaluator verified it by hand and said so.

    A subprocess rather than an inspection of `sys.modules`, because by the time this test
    runs the whole test session has already imported Textual for other reasons.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import remote_agents.bootstrap; "
        "print('textual' in sys.modules or any(m.startswith('textual.') for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False", (
        "importing the composition root pulled in Textual; `serve` now loads the terminal "
        "library it is meant to be isolated from"
    )


# --- The console's route back from a displayed agent (Sub-plan 3, Task 2.1) ------------
#
# With an agent's pane in the console's left slot, every key the owner types goes to that
# agent — so the way back has to be a *root* binding, and a root binding can only run a
# command. `remote-agents console projects` is that command.


def test_the_console_verb_asks_the_composer_for_the_projects_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    class _Composer:
        def __init__(self, *args, **kwargs) -> None:
            self.projects_command = kwargs.get("projects_command")

        async def show_projects(self) -> None:
            asked.append("show_projects")

    from remote_agents.application import console

    monkeypatch.setattr(console, "ConsoleComposer", _Composer)
    assert bootstrap.main(["console", "projects"]) == 0
    assert asked == ["show_projects"]


def test_the_console_verb_folds_the_panes_when_asked_for_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second console action, and the one a *key* reaches rather than a person.

    `prefix h` cannot fold panes by itself — the fold is eight resizes, a zoom and a window
    option a later exchange re-applies — so the key runs this verb, exactly as the way back
    from a displayed agent runs `console projects`.
    """
    asked: list[str] = []

    class _Composer:
        def __init__(self, *args, **kwargs) -> None:
            self.panes_command = kwargs.get("panes_command")

        async def toggle_panes(self) -> None:
            asked.append("toggle_panes")

    from remote_agents.application import console

    monkeypatch.setattr(console, "ConsoleComposer", _Composer)
    assert bootstrap.main(["console", "panes"]) == 0
    assert asked == ["toggle_panes"]


def test_the_fold_key_runs_this_interpreter_rather_than_a_name_on_path() -> None:
    """The same pipx failure the projects key already avoids, for the same reason."""
    import sys

    from remote_agents.composition import tui

    command = tui._panes_command()
    assert command[0] == sys.executable
    assert command[1:] == ("-m", "remote_agents", "console", "panes")


def test_an_unknown_console_action_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap, "_console_arrange", lambda action: pytest.fail("no such console action")
    )
    with pytest.raises(SystemExit) as refusal:
        bootstrap.main(["console", "rearrange-everything"])
    assert refusal.value.code != 0


def test_the_projects_key_runs_this_interpreter_rather_than_a_name_on_path() -> None:
    """A root binding that assumed a console script on PATH would work here and fail on pipx."""
    import sys

    from remote_agents.composition import tui

    command = tui._projects_command()
    assert command[0] == sys.executable
    assert command[1:] == ("-m", "remote_agents", "console", "projects")


def test_the_composition_gives_the_console_one_command_per_pane() -> None:
    """Without this the console builds the one-pane window it always built.

    Asserted against the composed object rather than bootstrap's source text, and it is the
    check that would have caught the shape the plan's own carried-in note complains about:
    `create_console` making a single pane while everything downstream assumed three.
    """
    import sys

    from remote_agents.ports.console import ConsolePaneSlot

    composer = bootstrap._console_composer(gateway=object(), home=None)
    commands = composer._pane_commands
    assert set(commands) == set(ConsolePaneSlot)
    for slot, command in commands.items():
        assert command[0] == sys.executable
        assert command[1:] == ("-m", "remote_agents", "pane", slot.name.lower())


# --- `remote-agents upgrade-sessions` (the repair the console names) -------------------


def test_the_upgrade_verb_reports_what_it_changed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from remote_agents.domain.models import SessionId

    upgraded = (SessionId.parse("04c709b1-06be-4b7b-b3bc-a4423b524718"),)

    class _Gateway:
        def __init__(self, *args, **kwargs) -> None: ...

        async def upgrade_pane_identity(self):
            return upgraded

    monkeypatch.setattr(bootstrap, "TmuxGateway", _Gateway)
    assert bootstrap.main(["upgrade-sessions"]) == 0
    out = capsys.readouterr().out
    assert "04c709b1-06be-4b7b-b3bc-a4423b524718" in out
    assert "1 session(s) upgraded" in out


def test_the_upgrade_verb_says_so_when_there_is_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Nothing happened" with no explanation is the failure this repair exists to end."""

    class _Gateway:
        def __init__(self, *args, **kwargs) -> None: ...

        async def upgrade_pane_identity(self):
            return ()

    monkeypatch.setattr(bootstrap, "TmuxGateway", _Gateway)
    assert bootstrap.main(["upgrade-sessions"]) == 0
    assert "already carries its identity" in capsys.readouterr().out


# `console close` is the third console verb, and the only one that destroys anything. F10
# launches it detached rather than running the teardown in the pane it is about to kill —
# a process inside the session it removes cannot finish, or report, if it dies half-way
# (DEC-096). Which means the exit status and the flash are the whole of what the owner sees.


def test_bootstrap_console_close_exits_zero_when_the_console_went_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both silent outcomes are exit zero: the owner asked for a gone console and has one."""
    from remote_agents.application import console
    from remote_agents.application.console import CloseOutcome, CloseReport

    for outcome in (CloseOutcome.CLOSED, CloseOutcome.NOTHING_TO_CLOSE):
        flashed: list[str] = []

        class _Composer:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def close(self) -> CloseReport:
                return CloseReport(outcome)

            async def flash(self, text: str) -> None:
                flashed.append(text)

        monkeypatch.setattr(console, "ConsoleComposer", _Composer)
        assert bootstrap.main(["console", "close"]) == 0, outcome
        # Nothing to say: the console is gone, which is what was asked for.
        assert flashed == [], outcome


def test_bootstrap_console_close_flashes_the_reason_and_exits_non_zero_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal is F10 appearing to do nothing, so the reason has to reach the owner's eyes.

    There is no terminal to print to that will outlive this: the verb runs detached, off any
    pane. The console's own status bar is the one surface still standing, which is what
    `flash` writes to — and a non-zero exit is what a script driving a deploy reads.
    """
    from remote_agents.application import console
    from remote_agents.application.console import CloseOutcome, CloseReport

    flashed: list[str] = []

    class _Composer:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def close(self) -> CloseReport:
            return CloseReport(CloseOutcome.REFUSED, "the console still shows an agent")

        async def flash(self, text: str) -> None:
            flashed.append(text)

    monkeypatch.setattr(console, "ConsoleComposer", _Composer)
    assert bootstrap.main(["console", "close"]) == 1
    assert flashed == ["the console still shows an agent"]


def test_bootstrap_console_close_did_not_open_the_closed_set_of_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a third choice must not turn the closed set into an open one."""
    monkeypatch.setattr(
        bootstrap, "_console_arrange", lambda action: pytest.fail("no such console action")
    )
    with pytest.raises(SystemExit) as refusal:
        bootstrap.main(["console", "bogus"])
    assert refusal.value.code != 0


def test_the_close_command_runs_this_interpreter_rather_than_a_name_on_path() -> None:
    """The same pipx trap the projects and fold commands already avoid, for the same reason.

    The console is started from whatever interpreter the owner installed this into, and an
    argv assuming a `remote-agents` console script on `PATH` works on a developer's host and
    fails on a pipx install — where it would mean F10 silently doing nothing at all.
    """
    import sys

    from remote_agents.composition import tui

    command = tui._close_command()
    assert command[0] == sys.executable
    assert command[1:] == ("-m", "remote_agents", "console", "close")


async def test_the_console_close_launcher_detaches_the_child_from_this_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`start_new_session=True` is the flag the whole teardown depends on, so it is asserted.

    `tests/integration/tmux/test_console_close_on_a_real_server.py` proves on real processes
    that a spawn made this way survives its parent being killed. It cannot prove that *this*
    launcher makes it that way — the verb it would have to call builds its composer from the
    hardcoded production socket (BL-041) — so the two halves are asserted apart: the flag here,
    the consequence there.

    Without it the closer sits in the pane's process group and tmux kills it mid-teardown:
    after the displayed agent was sent home and before the console was killed, or between the
    verify and the kill. Nothing would finish the job and nothing would report that it had not
    been finished.
    """
    import asyncio
    import sys

    from remote_agents.composition import tui

    seen: dict[str, object] = {}

    async def _record(*argv: str, **kwargs: object):
        seen["argv"] = argv
        seen.update(kwargs)
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _record)
    await tui._launch_console_close()

    assert seen["argv"] == (sys.executable, "-m", "remote_agents", "console", "close")
    assert seen["start_new_session"] is True, (
        "the closer would be killed with the pane that started it"
    )
    # The pty it would inherit belongs to the pane being handed back, so a write after the
    # session goes lands on a closed descriptor.
    assert seen["stdout"] is asyncio.subprocess.DEVNULL
    assert seen["stderr"] is asyncio.subprocess.DEVNULL
