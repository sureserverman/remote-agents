"""A surface drives the tmux server it runs on, and no other (DEC-144).

A surface inside a disposable console once classified itself as console-hosted and then drove
the owner's real console, because the composition root built its composer on the literal
production socket. These cases run every composer operation, and every gateway write a console
pane is wired with, against a runner that records each tmux argv, for a surface whose `$TMUX`
names a test socket — and require every argv to name that socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.codec import switch_client_argv
from remote_agents.domain.models import SessionId
from remote_agents.ports.tmux_server import SOCKET_NAME, hosting_socket

_TEST_SOCKET = "remote-agents-test-abc"
_SESSION = SessionId.parse("0b8f2c1e-3c4d-4e5f-8a9b-0c1d2e3f4a5b")


def _tmux(socket: str) -> str:
    return f"/tmp/tmux-1000/{socket},4242,0"


class RecordingRunner:
    """Answers every tmux call with empty output and keeps the argv."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.argvs.append(argv)
        return ""

    async def feed(self, data: str, *argv: str) -> str:
        self.argvs.append(argv)
        return ""


@pytest.fixture
def recorder(monkeypatch) -> RecordingRunner:
    import remote_agents.composition.tui as tui

    runner = RecordingRunner()
    monkeypatch.setattr(tui, "AsyncTmuxRunner", lambda *args, **kwargs: runner)
    return runner


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_tmux(_TEST_SOCKET), _TEST_SOCKET),
        (_tmux(SOCKET_NAME), SOCKET_NAME),
        (None, None),
        ("", None),
        (_tmux("default"), None),
        (_tmux("my-remote-agents"), None),
        ("remote-agents,4242,0", None),
        ("garbled", None),
        ("/tmp/a,b/tmux-1000/remote-agents-test-abc,4242,0", _TEST_SOCKET),
    ],
)
def test_the_hosting_socket_is_read_from_tmux_only_when_it_is_ours(value, expected) -> None:
    environment = {} if value is None else {"TMUX": value}
    assert hosting_socket(environment) == expected


async def _every_composer_operation(composer) -> dict[str, int]:
    """Run each operation and count the argvs it issued; a degraded one still issues its read."""
    issued: dict[str, int] = {}
    runner = composer._console._runner  # the shared recorder

    async def run(name, call):
        before = len(runner.argvs)
        try:
            await call()
        except Exception:  # noqa: BLE001 - empty tmux answers may fail an operation; argv counts
            pass
        issued[name] = len(runner.argvs) - before

    await run("ensure (bind keys)", composer.ensure)
    await run("settle (repair)", lambda: composer.settle(None))
    await run("show (swap)", lambda: composer.show(_SESSION))
    await run("show_projects", composer.show_projects)
    await run("show_settings", composer.show_settings)
    await run("toggle_panes", composer.toggle_panes)
    await run("hide", lambda: composer.hide(_SESSION))
    await run("sync", lambda: composer.sync(()))
    await run("flash", lambda: composer.flash("hello"))
    await run("close", composer.close)
    return issued


def _sockets(argvs) -> set[str]:
    return {argv[2] if argv[:2] == ("tmux", "-L") else "<no -L>" for argv in argvs}


@pytest.mark.parametrize("socket", [_TEST_SOCKET, SOCKET_NAME])
async def test_every_composer_operation_names_the_surface_s_own_server(
    socket, recorder, monkeypatch, tmp_path: Path
) -> None:
    from remote_agents.composition.tui import _console_composer

    monkeypatch.setenv("TMUX", _tmux(socket))
    composer = _console_composer(home=tmp_path)

    issued = await _every_composer_operation(composer)

    assert all(count > 0 for count in issued.values()), issued
    assert _sockets(recorder.argvs) == {socket}


@pytest.mark.parametrize("value", [None, _tmux("default")])
async def test_a_bare_shell_and_a_foreign_tmux_build_on_the_production_server(
    value, recorder, monkeypatch, tmp_path: Path
) -> None:
    from remote_agents.composition.tui import _console_composer

    if value is None:
        monkeypatch.delenv("TMUX", raising=False)
    else:
        monkeypatch.setenv("TMUX", value)
    composer = _console_composer(home=tmp_path)

    await _every_composer_operation(composer)

    assert _sockets(recorder.argvs) == {SOCKET_NAME}


async def test_the_surface_runtime_publishes_on_the_surface_s_own_server(
    recorder, monkeypatch, tmp_path: Path
) -> None:
    """The gateway a console pane publishes its selection and its bar flags through."""
    from remote_agents.composition.tui import _local_runtime
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("TMUX", _tmux(_TEST_SOCKET))
    runtime = _local_runtime(
        None, ProductionPaths.for_home(tmp_path), {}, folder_for=lambda record: None
    )
    gateway = runtime.gateway

    for call in (
        lambda: gateway.publish_selection(_SESSION),
        gateway.read_selection,
        lambda: gateway.publish_session_selected(True),
        lambda: gateway.publish_typing(True),
        lambda: gateway.holds_console_slot("%1"),
        lambda: gateway.fit_pane_height("%1", 3),
    ):
        try:
            await call()
        except Exception:  # noqa: BLE001 - empty tmux answers; the argv is what is asserted
            pass

    assert recorder.argvs
    assert _sockets(recorder.argvs) == {_TEST_SOCKET}


def test_the_switch_a_console_surface_execs_names_its_own_server(monkeypatch) -> None:
    from remote_agents.bootstrap import _switch_argv

    monkeypatch.setenv("TMUX", _tmux(_TEST_SOCKET))
    assert _switch_argv()(_SESSION)[:3] == ("tmux", "-L", _TEST_SOCKET)
    monkeypatch.delenv("TMUX")
    assert _switch_argv()(_SESSION)[:3] == ("tmux", "-L", SOCKET_NAME)
    assert switch_client_argv(_SESSION)[:3] == ("tmux", "-L", SOCKET_NAME)
