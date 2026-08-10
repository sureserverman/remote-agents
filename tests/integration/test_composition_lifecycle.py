"""Integration coverage for the composition lifecycle without network access."""

from __future__ import annotations

import asyncio

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
    config = AppConfig(home / "dev", home / "registry.yaml", paths.database_path, 40, 10)
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
