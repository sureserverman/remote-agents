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
    assert "Already in the console" in capsys.readouterr().out


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
