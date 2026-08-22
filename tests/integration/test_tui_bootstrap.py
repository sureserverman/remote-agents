"""The local terminal composes over the same private store the service uses."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.bootstrap import local_context, main
from remote_agents.config import ConfigError
from remote_agents.production import ProductionPaths

_REGISTRY = """version: 1
projects:
  - path: {existing}
    name: existing
    area: infra
    enabled: true
    added: 2026-07-30
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    (root / "dev" / "infra" / "existing").mkdir(parents=True)
    return root


@pytest.fixture
def paths(home: Path) -> ProductionPaths:
    return ProductionPaths.for_home(home)


def _config_file(
    home: Path, paths: ProductionPaths, database: Path | None = None, stem: str = "config"
) -> Path:
    registry = home / "projects-registry.yaml"
    registry.write_text(
        _REGISTRY.format(existing=home / "dev" / "infra" / "existing"), encoding="utf-8"
    )
    config_path = home / f"{stem}.toml"
    config_path.write_text(
        f'[paths]\ndev_root = "{home / "dev"}"\n'
        f'registry_path = "{registry}"\n'
        f'database_path = "{database or paths.database_path}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )
    return config_path


def test_the_local_context_offers_the_catalogue_profiles_and_creation_service(
    home: Path, paths: ProductionPaths, tmp_path: Path
) -> None:
    from remote_agents.config import load_config

    config = load_config(_config_file(home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        context = local_context(config, connection, paths)

        assert isinstance(context, TuiContext)
        assert "existing" in {project.name for project in context.backend.catalogue}
        assert {profile.profile_id for profile in context.profiles} == {
            "claude",
            "claude-remote",
            "codex",
            "cursor-agent",
            "opencode",
        }
        assert context.max_label_length == 40
        assert context.backend.projects.available_areas() == ("infra",)
    finally:
        connection.close()


def test_a_version_probe_that_timed_out_does_not_stop_the_surface_starting(
    home: Path, paths: ProductionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow host must not be an unstartable one.

    `probe_profiles` answers a probe that raised with `available=True` and the note
    `version_probe_failed` -- available because the executable resolved; the version is
    simply unread. `ProfileChoice` treats any reason on an available profile as a
    contradiction and refuses to construct, so `local_context` raised `ValueError: an
    available profile has no blocking reason` and the local surface would not start at all.

    Reached in the real world by `_run_version`'s `subprocess.run(..., timeout=5)` expiring
    under load -- `TimeoutExpired` is a `SubprocessError` -- which is a statement about the
    machine, not about whether the agent can be launched. DEC-002 already says local agent
    versions are owner-managed and do not gate launching, so the note must not arrive as a
    blocking reason.
    """
    from remote_agents.adapters.tmux import profiles as profiles_module
    from remote_agents.config import load_config

    def always_times_out(argv: tuple[str, ...]) -> str:
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=5)

    monkeypatch.setattr(profiles_module, "_run_version", always_times_out)

    config = load_config(_config_file(home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        context = local_context(config, connection, paths)

        assert context.profiles, "the surface must still be offered its profiles"
        for profile in context.profiles:
            assert not (profile.available and profile.reason), (
                f"{profile.profile_id} is available and carries a blocking reason"
            )
    finally:
        connection.close()


def test_the_local_context_needs_no_telegram_credentials(
    home: Path, paths: ProductionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner runs this on their own host; nothing about it is a remote control plane."""
    from remote_agents.config import load_config

    for name in (
        "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN",
        "REMOTE_AGENTS_OWNER_USER_ID",
        "REMOTE_AGENTS_OWNER_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    config = load_config(_config_file(home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        assert local_context(config, connection, paths) is not None
    finally:
        connection.close()


def test_the_tui_command_refuses_a_database_outside_the_private_state_directory(
    home: Path, paths: ProductionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal shares the service's store, so it inherits the same confinement."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config_path = _config_file(home, paths, database=tmp_path / "elsewhere.sqlite3")

    status = main(["tui", "--config", str(config_path)])

    assert status == 1


def test_serve_and_the_terminal_share_one_database_guard(
    home: Path, paths: ProductionPaths
) -> None:
    from remote_agents.bootstrap import _private_state_config

    outside = _config_file(home, paths, database=home / "elsewhere.sqlite3", stem="outside")
    inside = _config_file(home, paths, stem="inside")

    with pytest.raises(ConfigError):
        _private_state_config(outside, paths)
    assert _private_state_config(inside, paths).database_path == paths.database_path


async def test_two_connections_on_one_store_claim_distinct_keys(tmp_path: Path) -> None:
    """The service and the terminal are separate processes over one SQLite file."""
    database = tmp_path / "sessions.sqlite3"
    service = open_database(database)
    terminal = open_database(database)
    try:
        service_store = SQLiteSessionStore(service)
        terminal_store = SQLiteSessionStore(terminal)

        assert await service_store.claim_idempotency_key("service-1")
        assert await terminal_store.claim_idempotency_key("tui-1")
        assert not await terminal_store.claim_idempotency_key("service-1")
        assert not await service_store.claim_idempotency_key("tui-1")
    finally:
        service.close()
        terminal.close()


def test_textual_is_pinned_exactly_like_every_other_runtime_dependency() -> None:
    dependencies = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")

    assert '"textual==8.2.8"' in dependencies


def test_the_local_context_wires_the_two_stage_four_capabilities(
    home: Path, paths: ProductionPaths, tmp_path: Path
) -> None:
    """Asserted against the executed composition, not against bootstrap's source text.

    A source-substring check written for this missed it entirely: the string it looked for
    also appears in the *service* composition, so deleting both wirings from local_context
    left the whole suite green and would have shipped a terminal with no Inspect entry and
    no Resume flow.
    """
    from remote_agents.application.conversations import ConversationService
    from remote_agents.config import load_config

    config = load_config(_config_file(home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        context = local_context(config, connection, paths)

        assert context.backend.capture is not None, "the terminal cannot inspect without a capture"
        assert callable(context.backend.capture)
        assert isinstance(context.backend.conversations, ConversationService), (
            "the terminal cannot resume without a conversation service"
        )
        # No configuration key sources redactions today; the bot passes none either.
        assert context.capture_redactions == ()
    finally:
        connection.close()


# --- The pane surfaces compose exactly as `tui` does (Sub-plan 3, Stage 1) -------------
#
# Added at the Stage 1 gate. Both reviews found the same hole from opposite directions:
# `_enter_pane` was a hand-copied twin of the `tui` branch that no test ever drove, so the
# lease and the confinement were true by reading rather than by test, and the two copies had
# already begun to drift inside one stage. They are one body now, and these drive it.


def test_a_pane_refuses_a_database_outside_the_private_state_directory(
    home: Path, paths: ProductionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pane surface shares the service's store, so it inherits the same confinement."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config_path = _config_file(home, paths, database=tmp_path / "elsewhere.sqlite3")

    assert main(["pane", "projects", "--config", str(config_path)]) == 1


def test_a_pane_runs_over_a_lease_and_leaves_no_handle_behind(
    home: Path, paths: ProductionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-035, driven rather than read: the handle exists inside a store operation only.

    Three pane processes over one SQLite file is the whole premise of the three-pane console,
    and it is sound because none of them holds a standing connection. This runs a real pane
    entry point with the surface replaced by a probe, and asks the composed context what kind
    of connection it got.
    """
    from remote_agents.adapters.sqlite.database import LeasedConnection

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config_path = _config_file(home, paths)
    seen: list[TuiContext] = []

    def probe(context: TuiContext):
        seen.append(context)
        return None

    from remote_agents.adapters.tui import panes

    monkeypatch.setattr(panes, "run_pane_surface", lambda name, context: probe(context))
    assert main(["pane", "sessions", "--config", str(config_path)]) == 0

    assert len(seen) == 1
    store = seen[0].backend.sessions._store  # type: ignore[attr-defined]
    assert isinstance(store._connection, LeasedConnection), (
        "a pane surface must reach the store through the per-operation lease"
    )


def test_a_pane_that_fails_says_where_its_sessions_are_and_exits_nonzero(
    home: Path, paths: ProductionPaths, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A surface that dies must not take the owner's route to its sessions with it."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config_path = _config_file(home, paths)

    from remote_agents.adapters.tui import panes

    def explode(name, context):
        raise RuntimeError("the surface died")

    monkeypatch.setattr(panes, "run_pane_surface", explode)
    assert main(["pane", "feed", "--config", str(config_path)]) == 1
    assert "tmux -L remote-agents list-sessions" in capsys.readouterr().err
