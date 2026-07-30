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
