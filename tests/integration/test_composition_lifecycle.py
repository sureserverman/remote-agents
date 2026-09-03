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
    from remote_agents.config import AppConfig, load_secrets
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "11")
    home = tmp_path / "home"
    paths = ProductionPaths.for_home(home)
    paths.ensure_directories()
    (home / "dev").mkdir()
    config = AppConfig(home / "dev", home / "registry.yaml", paths.database_path, 40, 10, 30)
    connection = open_database(paths.database_path, migrations=MIGRATIONS)
    try:
        composition = _private_boundary(config, connection, paths, load_secrets())
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
    from remote_agents.config import AppConfig, load_secrets
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "11")
    home = tmp_path / "home"
    paths = ProductionPaths.for_home(home)
    paths.ensure_directories()
    (home / "dev").mkdir()
    config = AppConfig(home / "dev", home / "registry.yaml", paths.database_path, 40, 10, 30)
    connection = open_database(paths.database_path, migrations=MIGRATIONS)
    try:
        composition = _private_boundary(config, connection, paths, load_secrets())
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


_STATED_CEILING = 314_159
"""A ceiling no default could coincide with, so the wiring assertion cannot be a tautology."""


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
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n"
        # A ceiling that is nothing's default, so a test asserting it survives the wiring is
        # asserting something. With the key absent this file's `claude_context_window` was the
        # module default, and the wiring test below reduced to `[1000000] == [1000000]` --
        # replacing the whole composition with a hardcoded constant passed all 3453 tests.
        f"claude_context_window = {_STATED_CEILING}\n",
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
    from remote_agents.composition.backend import compose_backend
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
        assert backend.host_remote_control is not None, (
            "no host remote control: codex declares the capability, so a real composition "
            "must carry it -- a None here would render 'unavailable' on a host that has it"
        )
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
        assert backend.limits is not None
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
    from remote_agents.composition.backend import compose_backend
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
    from remote_agents.composition.backend import compose_backend
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
    from remote_agents.composition.backend import compose_backend
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
    from remote_agents.config import load_config, load_secrets
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "1:aa")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "7")
    monkeypatch.delenv("TMUX", raising=False)

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        composition = _private_boundary(config, connection, paths, load_secrets())
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
    from remote_agents.config import load_config, load_secrets
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "1:aa")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "7")

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        composition = _private_boundary(config, connection, paths, load_secrets())

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

    Sub-plan 4 made that line the correct one. `composition.backend._narrow_profiles`
    narrows once, into
    `application.profiles.ProfileAvailability`, and both surfaces read `Backend.profiles` --
    so what this now asserts is that the narrowing still happens *before* the boundary, and
    that a future edit cannot quietly put the domain tuple back on the field. The assertion is
    unchanged; only which direction it is guarding is.
    """
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.application.profiles import ProfileAvailability
    from remote_agents.bootstrap import _private_boundary
    from remote_agents.config import load_config, load_secrets
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "1:aa")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "7")

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        composition = _private_boundary(config, connection, paths, load_secrets())

        assert composition.boundary.profiles, "the wizard was offered no profiles at all"
        for profile in composition.boundary.profiles:
            assert isinstance(profile, ProfileAvailability), (
                "the bot was handed the domain ProfileCompatibility rather than the one "
                "narrowing both surfaces share -- see Backend.profiles"
            )
    finally:
        connection.close()


def test_compose_backend_builds_one_set_of_provider_readers(composed_home, tmp_path, monkeypatch):
    """DEC-046, asserted rather than asserted-about.

    The sibling test above carried a comment claiming the session read and the account read
    share their provider readers, while asserting only that `limits` was non-None -- which
    stayed green when `compose_backend` was mutated to build two sets. A comment is not a
    check, and the property it named is the one DEC-046 is about: a host should probe for
    provider files once per process, not once per capability.
    """
    from remote_agents.adapters.agents import registry as registry_module
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.composition.backend import compose_backend
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    built = []

    class _Counted(registry_module.ProfileUsageReaders):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr("remote_agents.adapters.agents.registry.ProfileUsageReaders", _Counted)

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        backend = compose_backend(config, connection, paths)

        assert len(built) == 1, f"composed {len(built)} reader sets, not one"
        assert backend.usage is not None and backend.limits is not None
    finally:
        connection.close()


def test_compose_backend_hands_the_readers_the_declared_ceiling(
    composed_home, tmp_path, monkeypatch
):
    """The owner's number has to survive the whole way to the reader, or it renders nothing.

    Wired here rather than read by the adapter, because `config` is where the declaration lives
    and the composition root is the one place allowed to know both. A ceiling that stopped at
    the config would leave every Claude row a bare count while the owner believed they had
    stated one -- a failure with no error to notice.
    """
    from remote_agents.adapters.agents import registry as registry_module
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.composition.backend import compose_backend
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    seen = []

    class _Recording(registry_module.ClaudeUsageReader):
        def __init__(self, **passed):
            # `**passed` rather than a copied signature: a double that restates the real one
            # goes stale the moment an argument is added, and fails with a TypeError that reads
            # like a product bug rather than a test that was not updated.
            seen.append(passed)
            super().__init__(**passed)

    monkeypatch.setattr("remote_agents.adapters.agents.claude.ClaudeUsageReader", _Recording)

    paths = ProductionPaths.for_home(composed_home)
    config = load_config(_config_file(composed_home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        compose_backend(config, connection, paths)

        # Against the literal as well as the config, so this cannot pass by both sides being
        # the same default.
        assert seen == [{"context_window": _STATED_CEILING, "context_window_stated": True}]
        assert config.claude_context_window == _STATED_CEILING
    finally:
        connection.close()


def test_an_unstated_ceiling_never_reaches_the_reader(composed_home, tmp_path, monkeypatch):
    """A host that declared nothing gets no ceiling, and so no percentage against one.

    Passing the default unconditionally made `ClaudeUsageReader`'s bare-count path unreachable
    in production, so every Claude row on such a host rendered a percentage against this
    project's assumption. On a 200k plan that reads 68% for a context 340% full, with no tell on
    either surface -- which is the invented number DEC-061 forbids, arriving through the config
    layer rather than through the reader.
    """
    from remote_agents.adapters.agents import registry as registry_module
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.composition.backend import compose_backend
    from remote_agents.config import load_config
    from remote_agents.production import ProductionPaths

    seen = []

    class _Recording(registry_module.ClaudeUsageReader):
        def __init__(self, **passed):
            seen.append(passed)
            super().__init__(**passed)

    monkeypatch.setattr("remote_agents.adapters.agents.claude.ClaudeUsageReader", _Recording)

    paths = ProductionPaths.for_home(composed_home)
    config_path = _config_file(composed_home, paths)
    # The deployed shape: every other key, and no ceiling.
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f"claude_context_window = {_STATED_CEILING}\n", ""
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        compose_backend(config, connection, paths)

        assert config.claude_context_window_stated is False
        assert seen == [{"context_window": None, "context_window_stated": False}]
    finally:
        connection.close()


async def test_the_composition_names_the_watcher_for_what_it_watches() -> None:
    """The composition no longer offers a field, a coroutine or a log line about pane quiet.

    Named here rather than left to the grep in the Stage 1 gate because the failure this
    prevents is not a stale identifier: `ServiceComposition` is constructed positionally in
    seven places, so a field that keeps a retired name is the thing a reader consults to find
    out what the service does, and it would go on describing a digest watch that was deleted.
    The coroutine pair is the same argument one level down -- `_watch_quiet_once` is what an
    operator greps for when the activity pass misbehaves.
    """
    from dataclasses import fields

    from remote_agents.composition import service

    names = {field.name for field in fields(service.ServiceComposition)}
    assert "approval_watcher" in names
    assert "quiet_watcher" not in names
    assert not hasattr(service, "_watch_quiet_once")
    assert not hasattr(service, "_watch_quiet_periodically")
    assert hasattr(service, "_watch_activity_once")
    assert hasattr(service, "_watch_activity_periodically")


async def test_a_claude_only_host_still_runs_its_activity_pass_with_no_watcher() -> None:
    """The pass is gated on *either* source, and a spool alone has to be enough.

    A host running only Claude sessions has no pane anything can observe -- `UNOBSERVED` and
    `HOOK_EXCLUSIVE` are both skipped by the narrowed watcher -- and a spool full of what those
    sessions reported. Gating the periodic task on the watcher would deliver none of it.
    """
    import inspect

    from remote_agents.composition import service

    source = inspect.getsource(service._serve_with_reconciliation)
    assert "composition.approval_watcher is not None or composition.activity_directory" in source


def test_a_registry_declaring_no_host_remote_control_composes_none():
    """Absence is representable, because a host whose providers wire none must say so.

    The mirror of the assertion above, and the reason `Backend.host_remote_control` is
    optional at all: `None` is what a frontend reads with `is None` to render "unavailable"
    (DEC-061/067), so a composition that could not produce one would make that branch dead.
    """
    from remote_agents.composition.backend import _host_remote_control
    from remote_agents.domain.models import ProfileId
    from remote_agents.ports.provider_descriptor import ProviderDescriptor

    barren = (
        ProviderDescriptor(ProfileId("claude")),
        ProviderDescriptor(ProfileId("opencode")),
    )
    assert _host_remote_control(barren, store=object(), locks=object()) is None


def test_exactly_one_descriptor_may_own_the_host_toggle():
    """The subject is the machine, so two providers claiming it is a wiring bug, not a merge.

    Refused rather than resolved by order: picking the first would make the composition's
    behaviour depend on registry iteration order, which is exactly the kind of silent
    tie-break that only shows up on the host where the order differs.
    """
    import pytest

    from remote_agents.composition.backend import _host_remote_control
    from remote_agents.domain.models import ProfileId
    from remote_agents.ports.provider_descriptor import ProviderDescriptor

    doubled = (
        ProviderDescriptor(ProfileId("codex"), remote_control=object()),
        ProviderDescriptor(ProfileId("opencode"), remote_control=object()),
    )
    with pytest.raises(ValueError):
        _host_remote_control(doubled, store=object(), locks=object())


async def test_the_host_toggle_is_drained_by_the_same_locks_as_the_session_service():
    """The composition's claim that both use cases share one drain, made checkable.

    `compose_backend` passes one `SessionLocks` to `SessionService` and to the host toggle so
    that a daemon enable in flight is counted by the same drain that finishes a stop. Nothing
    asserted that before: handing the host service a fresh `SessionLocks()` passed every test
    in this suite while quietly giving the process two drains, neither covering the other.
    """
    import asyncio

    from remote_agents.application.host_remote_control import HostRemoteControlCommand
    from remote_agents.application.reconcile import SessionLocks
    from remote_agents.composition.backend import _host_remote_control
    from remote_agents.domain.models import ProfileId
    from remote_agents.domain.remote_control import (
        HostConnection,
        HostRemoteControlStatus,
        RemoteControlState,
    )
    from remote_agents.ports.provider_descriptor import ProviderDescriptor

    gate = asyncio.Event()

    class BlockingControl:
        async def set_state(self, desired):
            await gate.wait()
            return HostRemoteControlStatus.observed(
                HostConnection.CONNECTED, server_name="Paisleys-Blender"
            )

    class Store:
        async def claim_idempotency_key(self, key):
            return True

    locks = SessionLocks()
    service = _host_remote_control(
        (ProviderDescriptor(ProfileId("codex"), remote_control=BlockingControl()),),
        store=Store(),
        locks=locks,
    )

    task = asyncio.create_task(
        service.set_state(HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1"))
    )
    await asyncio.sleep(0)
    drain = asyncio.create_task(locks.drain())
    await asyncio.sleep(0)

    assert not drain.done(), (
        "the drain finished while a host toggle was open, so the composition handed the "
        "host use case a lock set the session service does not share"
    )
    gate.set()
    await task
    await drain
