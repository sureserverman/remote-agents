"""Integration coverage for the composition lifecycle without network access."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from remote_agents.application.runtime import RuntimeCoordinator


class FakePolling:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.started = asyncio.Event()
        self._stopped = asyncio.Event()

    async def run_forever(self) -> None:
        self.events.append("polling-start")
        self.started.set()
        await self._stopped.wait()
        self.events.append("telegram-shutdown")

    def request_stop(self) -> None:
        self.events.append("polling-stop-requested")
        self._stopped.set()


class FakeTerminal:
    async def managed_observations(self) -> tuple[str, ...]:
        return ("trusted-session",)


class FakeReconciler:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.observations: list[tuple[str, ...]] = []

    async def reconcile(self, observations: tuple[str, ...]) -> None:
        self.events.append("reconcile")
        self.observations.append(observations)


class FakeDrainer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def drain(self) -> None:
        self.events.append("mutations-drained")


async def test_runtime_reconciles_before_polling_and_drains_after_telegram_shutdown() -> None:
    events: list[str] = []
    polling = FakePolling(events)
    reconciler = FakeReconciler(events)
    runtime = RuntimeCoordinator(
        polling=polling,
        reconciler=reconciler,
        terminal=FakeTerminal(),
        drainer=FakeDrainer(events),
        reconcile_interval=3600,
    )

    task = asyncio.create_task(runtime.run())
    await polling.started.wait()

    assert events[:2] == ["reconcile", "polling-start"]
    assert reconciler.observations == [("trusted-session",)]

    runtime.request_stop()
    await task

    assert events[-3:] == [
        "polling-stop-requested",
        "telegram-shutdown",
        "mutations-drained",
    ]


async def test_runtime_propagates_periodic_reconciliation_failure_and_stops_polling() -> None:
    events: list[str] = []
    polling = FakePolling(events)
    reconciler = FailingReconciler(events)
    runtime = RuntimeCoordinator(
        polling=polling,
        reconciler=reconciler,
        terminal=FakeTerminal(),
        drainer=FakeDrainer(events),
        reconcile_interval=0,
    )

    with pytest.raises(RuntimeError, match="synthetic reconciliation failure"):
        await runtime.run()

    assert events == [
        "reconcile",
        "polling-start",
        "reconcile",
        "polling-stop-requested",
        "telegram-shutdown",
        "mutations-drained",
    ]


class FailingReconciler(FakeReconciler):
    async def reconcile(self, observations: tuple[str, ...]) -> None:
        await super().reconcile(observations)
        if len(self.observations) == 2:
            raise RuntimeError("synthetic reconciliation failure")


def test_the_service_composition_gives_the_bot_a_durable_callback_store(
    tmp_path, monkeypatch
) -> None:
    """One keyword argument is the whole fix for "my buttons stop working after a restart".

    `PrivateBotBoundary` still defaults to the in-memory store, which is right for a
    composition with no database — and means a composition that forgets to pass the durable
    one gets the old defect back silently, with every test still green. This pins the line.
    """
    from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
    from remote_agents.adapters.sqlite.chat_view_store import SQLiteChatViewStore
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS
    from remote_agents.adapters.sqlite.standing_notification_store import (
        SQLiteStandingNotificationStore,
    )
    from remote_agents.bootstrap import _private_boundary
    from remote_agents.config import AppConfig
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "11")
    home = tmp_path / "home"
    paths = ProductionPaths.for_home(home)
    paths.ensure_directories()
    (home / "dev").mkdir()
    config = AppConfig(home / "dev", home / "registry.yaml", paths.database_path, 40, 10, 30, 3)
    connection = open_database(paths.database_path, migrations=MIGRATIONS)
    try:
        composition = _private_boundary(config, connection, paths)
    finally:
        connection.close()

    assert isinstance(composition.boundary.callbacks, SQLiteCallbackStateStore)
    # Both halves of the durable pair, not just the one. `callbacks` and `anchors` are the
    # only two boundary fields that fall back to an in-memory store when the wiring is
    # dropped, and a fallback is silent — the suite stayed green with `anchors` deleted from
    # `bootstrap`, which is the restart defect back: a forgotten anchor sends a second live
    # view and leaves the first above it, still holding buttons that resolve.
    assert isinstance(composition.boundary.anchors, SQLiteChatViewStore)
    # And the third silent fallback, added for the same reason the two above are pinned: the
    # standing notification each session owns is durable, so a restart amends the message
    # already in the chat instead of sending a second one beside it.
    assert isinstance(composition.boundary.standing, SQLiteStandingNotificationStore)


def test_the_service_composition_lets_the_bot_step_the_console_aside(tmp_path, monkeypatch) -> None:
    """A stop from the phone must move the console *before* it destroys the pane.

    Without this one keyword argument the bot ends the session and leaves the agent's pane to
    be killed inside the console's own window — so the console sits a pane short, sessions and
    feed stretched across the full width, until its next reload puts the projects surface back
    up to ten seconds later. `SessionService` defaults it to None and degrades silently, which
    is right for a composition with no console and is exactly why the wiring needs pinning.

    The console lock is asserted alongside it, because the two are one change: a second process
    arranging these panes is only safe while both composers name the same file, and they do
    that by both coming from `_console_composer`.
    """
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS
    from remote_agents.bootstrap import _console_composer, _private_boundary
    from remote_agents.config import AppConfig
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "11")
    home = tmp_path / "home"
    paths = ProductionPaths.for_home(home)
    paths.ensure_directories()
    (home / "dev").mkdir()
    config = AppConfig(home / "dev", home / "registry.yaml", paths.database_path, 40, 10, 30, 3)
    connection = open_database(paths.database_path, migrations=MIGRATIONS)
    try:
        composition = _private_boundary(config, connection, paths)
    finally:
        connection.close()

    launcher = composition.boundary.backend.sessions
    assert launcher._hide_in_console is not None, "a phone stop cannot move the console"
    # The same file the local surface's composer takes, or the lock excludes nothing.
    assert (
        _console_composer(home=home)._links._path
        == ProductionPaths.for_home(home).console_lock_path
    )


# --- One backend, composed once per process (ARCH-B1, ARCH-B2) -----------------------

_REGISTRY = """version: 1
projects:
  - path: {existing}
    name: existing
    area: infra
    enabled: true
    added: 2026-07-30
"""


def _config_file(home, paths):
    registry = home / "projects-registry.yaml"
    registry.write_text(
        _REGISTRY.format(existing=home / "dev" / "infra" / "existing"), encoding="utf-8"
    )
    config_path = home / "config.toml"
    config_path.write_text(
        f'[paths]\ndev_root = "{home / "dev"}"\n'
        f'registry_path = "{registry}"\n'
        f'database_path = "{paths.database_path}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )
    return Path(config_path)


@pytest.fixture
def composed_home(tmp_path):
    root = tmp_path / "home"
    (root / "dev" / "infra" / "existing").mkdir(parents=True)
    return root


def test_compose_backend_builds_one_backend_from_the_real_helpers(composed_home, tmp_path):
    """Both surfaces are composed from this, so it must carry what either one drives.

    The helpers it reuses (`_local_runtime`, `_conversation_service`, `_project_creator`,
    `ProjectCatalogueProvider`) are the ones the two compositions already shared; what
    changes is that the sharing is now a named function rather than four call sites that
    happened to agree.
    """
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.application.backend import Backend
    from remote_agents.bootstrap import compose_backend
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        backend = compose_backend(config, connection, paths)

        assert isinstance(backend, Backend)
        assert backend.sessions is not None, "no session lifecycle use case"
        assert backend.projects is not None, "no project creation use case"
        assert backend.conversations is not None, "no resume service"
        assert "existing" in {project.name for project in backend.catalogue}
        assert {str(profile.profile_id) for profile in backend.profiles} == {
            "claude",
            "claude-remote",
            "codex",
            "cursor-agent",
            "opencode",
        }
        assert backend.max_label_length == 40
        assert callable(backend.refresh_catalogue)
        assert "existing" in {project.name for project in backend.refresh_catalogue()}
        assert backend.capture is not None
    finally:
        connection.close()


def test_compose_backend_opens_no_connection_of_its_own(composed_home, tmp_path):
    """ARCH-B2: the connection strategy is the caller's, and DEC-035 depends on it.

    `serve` holds one connection for the life of the process; a surface holds one only for
    the duration of a single store operation (`LeasedConnection`). If `compose_backend`
    opened its own, a surface would acquire a handle it never agreed to hold, and the
    guarantee the README states in those words would be false. Proven by handing it a
    connection and asserting the backend uses that object.
    """
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.bootstrap import compose_backend
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        backend = compose_backend(config, connection, paths)
        store = backend.sessions._store  # noqa: SLF001 -- proving which handle it holds
        assert store._connection is connection, (  # noqa: SLF001
            "compose_backend must use the connection it was given, never open one"
        )
    finally:
        connection.close()


async def test_a_leased_backend_holds_no_handle_between_operations(composed_home, tmp_path):
    """DEC-035, the half a surface must keep: the handle lasts one store operation.

    The local surface is long-lived beside attached sessions, so what replaced the old
    exec-away contract is this narrower guarantee — and the README states it in those
    words. A `compose_backend` that opened or cached a connection would falsify it silently,
    because every test above would still pass: the backend would work, it would simply be
    holding something it promised not to.
    """
    from remote_agents.adapters.sqlite.database import leased_connection, open_database
    from remote_agents.bootstrap import compose_backend
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    database = tmp_path / "sessions.sqlite3"
    open_database(database).close()

    lease = leased_connection(database)
    backend = compose_backend(config, lease, paths)
    # Identity first, and it is the load-bearing half. Without it the two `_held`
    # assertions below are vacuous: they would still pass if `compose_backend` special-cased
    # a LeasedConnection and opened a real one instead, because nothing would then touch
    # `lease` at all. That is precisely the DEC-035 regression this task guards.
    assert backend.sessions._store._connection is lease, (  # noqa: SLF001
        "the backend is not using the lease it was given"
    )
    assert lease._held is None, "composing alone acquired a handle"  # noqa: SLF001

    await backend.sessions.list_sessions()

    assert lease._held is None, (  # noqa: SLF001
        "the surface kept a database handle between store operations (DEC-035)"
    )


async def test_a_long_lived_backend_keeps_the_one_handle_it_was_given(composed_home, tmp_path):
    """The other half: the serve composition's connection is not re-opened per operation.

    The store cannot tell which composition it is running under, and that is the point —
    the strategy lives entirely in what the caller hands to `compose_backend`.
    """
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.bootstrap import compose_backend
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        backend = compose_backend(config, connection, paths)

        await backend.sessions.list_sessions()
        assert backend.sessions._store._connection is connection  # noqa: SLF001
        await backend.sessions.list_sessions()
        assert backend.sessions._store._connection is connection, (  # noqa: SLF001
            "the serve composition's one long-lived connection was replaced"
        )
    finally:
        connection.close()


def test_both_compositions_wire_hide_in_console_from_their_own_composers(
    composed_home, tmp_path, monkeypatch
):
    """ARCH-B3, as corrected at the Stage 1 gate.

    The plan originally said `hide_in_console` is the surface's alone. It is not, and has
    not been since the console-lock work: the bot builds a composer too, so a stop issued
    from the phone steps the console aside before destroying the pane. The asymmetry is in
    what each composer may do -- the surface's builds and arranges the console, the bot's is
    hide-only and never calls `ensure` -- not in who has one.

    Pinned because a `Backend` shared by both processes is exactly the change that would
    tempt someone to give them one composer, and the bot must never gain `ensure`: it would
    build the owner's console from a process that has no window in it.
    """
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.bootstrap import _private_boundary, local_context
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "1:aa")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "7")
    monkeypatch.delenv("TMUX", raising=False)

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        composition = _private_boundary(config, connection, paths)
        context = local_context(config, connection, paths)

        assert composition.boundary.backend.sessions._hide_in_console is not None, (  # noqa: SLF001
            "the bot lost its hide-only composer; a phone stop would leave the console "
            "a pane short until the next sync"
        )
        # Not console-hosted here ($TMUX unset), so the surface wires none -- which is the
        # other half of the same rule: the surface's composer is conditional on hosting.
        assert context.backend.sessions._hide_in_console is None  # noqa: SLF001
    finally:
        connection.close()


def test_the_reconciler_and_the_backend_share_one_lock_map(composed_home, tmp_path, monkeypatch):
    """DEC-030, and it was a production incident, not a theory.

    The reconciler runs on a timer beside the service and writes `record_event` directly,
    so without a lock in common it overwrites the state of a session whose graceful stop is
    between its own two writes -- the InvalidTransition crashes. Constructing two
    `SessionLocks` here would type-check, run, and fix nothing, which is exactly why this is
    pinned rather than left to the comment beside it.

    Pinned *now* because the refactor put one more layer of indirection between the
    `SessionLocks()` call and the service: the locks reach `backend.sessions` through
    `compose_backend`, so a future edit to its default handling (`locks or SessionLocks()`
    lives inside `SessionService`) could hand the backend a private map while the reconciler
    keeps the shared one, and nothing would fail.
    """
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.bootstrap import _private_boundary
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "1:aa")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "7")

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        composition = _private_boundary(config, connection, paths)

        assert composition.boundary.backend.sessions._locks is composition.reconciler._locks, (  # noqa: SLF001
            "the service and the reconciler hold different lock maps (DEC-030)"
        )
    finally:
        connection.close()


def test_the_bot_is_offered_the_narrowed_profiles_not_the_domain_ones(
    composed_home, tmp_path, monkeypatch
):
    """The bot is handed the narrowed type, never the domain record.

    **This pin has outlived the bug it was written for, and now guards the repair.** It was
    written when `Backend.profiles` carried the domain `ProfileCompatibility`, whose `reason`
    field answers two questions at once, and when each surface narrowed that separately with a
    type that read any reason as blocking. Back then `profiles=backend.profiles` was the
    plausible-looking line that would have taken the local surface down on a probe that merely
    timed out, and this asserted nobody had written it.

    Sub-plan 4 made that line the correct one. `bootstrap._narrow_profiles` narrows once, into
    `application.profiles.ProfileAvailability`, and both surfaces read `Backend.profiles` --
    so what this now asserts is that the narrowing still happens *before* the boundary, and
    that a future edit cannot quietly put the domain tuple back on the field. The assertion is
    unchanged; only which direction it is guarding is.
    """
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.application.profiles import ProfileAvailability
    from remote_agents.bootstrap import _private_boundary
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "1:aa")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "7")

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        composition = _private_boundary(config, connection, paths)

        assert composition.boundary.profiles, "the wizard was offered no profiles at all"
        for profile in composition.boundary.profiles:
            assert isinstance(profile, ProfileAvailability), (
                "the bot was handed the domain ProfileCompatibility rather than the one "
                "narrowing both surfaces share -- see Backend.profiles"
            )
    finally:
        connection.close()
