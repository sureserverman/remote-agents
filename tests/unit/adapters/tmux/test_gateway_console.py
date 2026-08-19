"""Console operations widen the gateway without widening what `mutate` accepts.

Every console operation is its own named method over codec-generated targets, exactly the
discipline `kill-session` set: no free-text target ever reaches the runner, and the generic
`mutate` entry still refuses everything it refused before this file existed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId

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


async def test_the_forbidden_operation_rule_is_unchanged() -> None:
    runner = RecordingRunner()
    for operation in ("link-window", "switch-client", "kill-server", "unlink-window"):
        with pytest.raises(ValueError):
            await gateway(runner).mutate(operation, f"ra-{_SESSION}")
    assert runner.calls == []


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


async def test_link_marks_the_source_window_before_linking_it() -> None:
    runner = RecordingRunner()
    await gateway(runner).link_session_window(_SESSION)
    assert runner.calls == [
        (
            *_BASE,
            "set-option",
            "-w",
            "-t",
            _EXACT,
            "@remote_agents_window_session",
            str(_SESSION),
        ),
        (*_BASE, "link-window", "-s", _EXACT, "-t", "ra-console:"),
    ]


async def test_unlink_names_one_tab_and_the_dashboard_is_not_one() -> None:
    runner = RecordingRunner()
    await gateway(runner).unlink_console_window(2)
    assert runner.calls == [(*_BASE, "unlink-window", "-t", "ra-console:2")]
    with pytest.raises(ValueError):
        await gateway(runner).unlink_console_window(0)


async def test_console_windows_decodes_the_pinned_mapping() -> None:
    runner = RecordingRunner(output=f"0|\n1|{_SESSION}\n")
    assert await gateway(runner).console_windows() == ((0, None), (1, _SESSION))
    assert runner.calls == [
        (
            *_BASE,
            "list-windows",
            "-t",
            "ra-console:",
            "-F",
            "#{window_index}|#{@remote_agents_window_session}",
        )
    ]


async def test_console_windows_reads_an_absent_console_as_empty() -> None:
    for message in ("no server running on /tmp/x", "can't find session: ra-console"):
        runner = RecordingRunner(error=RuntimeError(message))
        assert await gateway(runner).console_windows() == ()


async def test_focus_and_switch_operations_use_generated_targets_only() -> None:
    runner = RecordingRunner()
    g = gateway(runner)
    await g.select_console_window(0)
    await g.switch_client_to_session(_SESSION)
    await g.switch_client_to_console()
    await g.display_message("agent finished: opaque-editor")
    assert runner.calls == [
        (*_BASE, "select-window", "-t", "ra-console:0"),
        (*_BASE, "switch-client", "-t", _EXACT),
        (*_BASE, "switch-client", "-t", "ra-console:"),
        (*_BASE, "display-message", "-l", "--", "agent finished: opaque-editor"),
    ]


async def test_the_console_binding_is_installed_on_our_socket_with_a_validated_key() -> None:
    runner = RecordingRunner()
    await gateway(runner).install_console_binding("F12")
    await gateway(runner).install_console_binding("C-a")
    assert runner.calls == [
        (*_BASE, "bind-key", "-n", "F12", "select-window", "-t", "ra-console:0"),
        (*_BASE, "bind-key", "-n", "C-a", "select-window", "-t", "ra-console:0"),
    ]
    for key in ("", "two words", "a;b", "$(rm)", "péché", "C-", "C-;"):
        with pytest.raises(ValueError):
            await gateway(runner).install_console_binding(key)


async def test_a_gone_target_is_typed_for_every_single_target_console_operation() -> None:
    """The race capture()/mutate() were built for — the object vanishing between the
    caller's decision and the call landing — gets the same TerminalTargetMissing typing
    on every new single-target operation, so existing handlers catch it uniformly."""
    from remote_agents.ports.terminal import TerminalTargetMissing

    gone = RuntimeError("can't find session: whatever")
    with pytest.raises(TerminalTargetMissing):
        await gateway(RecordingRunner(error=gone)).unlink_console_window(2)
    with pytest.raises(TerminalTargetMissing):
        await gateway(RecordingRunner(error=gone)).link_session_window(_SESSION)
    with pytest.raises(TerminalTargetMissing):
        await gateway(RecordingRunner(error=gone)).switch_client_to_session(_SESSION)
    with pytest.raises(TerminalTargetMissing):
        await gateway(RecordingRunner(error=gone)).select_console_window(1)
    with pytest.raises(TerminalTargetMissing):
        await gateway(RecordingRunner(error=gone)).switch_client_to_console()


async def test_a_broken_tmux_is_never_misread_as_an_absent_console() -> None:
    """The unmatched-error branch: anything that is not an absent server or target keeps
    its type and propagates — the failure mode the inventory docstring warns about."""
    broken = RuntimeError("server exited unexpectedly")
    with pytest.raises(RuntimeError, match="server exited unexpectedly"):
        await gateway(RecordingRunner(error=broken)).console_exists()
    with pytest.raises(RuntimeError, match="server exited unexpectedly"):
        await gateway(RecordingRunner(error=broken)).console_windows()


async def test_the_active_window_probe_parses_and_degrades_per_branch() -> None:
    """Every branch of the flash's window probe: a number parses, garbage is None (a
    broken proxy must read as 'unknown', never crash the flash), an absent console or
    server is a plain None, and a genuinely broken tmux keeps its error type."""
    assert await gateway(RecordingRunner(output="2\n")).console_active_window() == 2
    assert await gateway(RecordingRunner(output="0\n")).console_active_window() == 0
    assert await gateway(RecordingRunner(output="garbage")).console_active_window() is None
    for message in ("no server running on /tmp/x", "can't find session: ra-console"):
        absent = RecordingRunner(error=RuntimeError(message))
        assert await gateway(absent).console_active_window() is None
    with pytest.raises(RuntimeError, match="server exited"):
        await gateway(
            RecordingRunner(error=RuntimeError("server exited unexpectedly"))
        ).console_active_window()
