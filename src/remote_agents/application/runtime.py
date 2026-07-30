"""Orderly runtime orchestration for composed adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol


class PollingRuntime(Protocol):
    async def run_forever(self) -> None: ...

    def request_stop(self) -> None: ...


class Reconciler(Protocol):
    async def reconcile(self, observations: tuple[object, ...]) -> object: ...


class ManagedTerminal(Protocol):
    async def managed_observations(self) -> tuple[object, ...]: ...


class MutationDrainer(Protocol):
    async def drain(self) -> None: ...


class RuntimeCoordinator:
    """Reconcile before polling, propagate failures, then stop and drain in order."""

    def __init__(
        self,
        *,
        polling: PollingRuntime,
        reconciler: Reconciler,
        terminal: ManagedTerminal,
        drainer: MutationDrainer,
        reconcile_interval: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if reconcile_interval < 0:
            raise ValueError("reconciliation interval cannot be negative")
        self._polling = polling
        self._reconciler = reconciler
        self._terminal = terminal
        self._drainer = drainer
        self._reconcile_interval = reconcile_interval
        self._sleep = sleep
        self._stop_requested = asyncio.Event()

    def request_stop(self) -> None:
        """Request orderly shutdown without touching managed terminal sessions."""
        self._stop_requested.set()

    async def run(self) -> None:
        """Run polling and periodic reconciliation until requested stop or failure."""
        await self._reconcile_once()
        polling_task = asyncio.create_task(self._polling.run_forever())
        periodic_task = asyncio.create_task(self._reconcile_periodically())
        stop_task = asyncio.create_task(self._stop_requested.wait())
        try:
            done, _ = await asyncio.wait(
                (polling_task, periodic_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                return
            for task in done:
                if task is not stop_task:
                    task.result()
            raise RuntimeError("polling stopped unexpectedly")
        finally:
            self._polling.request_stop()
            periodic_task.cancel()
            stop_task.cancel()
            await asyncio.gather(polling_task, periodic_task, stop_task, return_exceptions=True)
            await self._drainer.drain()

    async def _reconcile_once(self) -> None:
        await self._reconciler.reconcile(await self._terminal.managed_observations())

    async def _reconcile_periodically(self) -> None:
        while True:
            await self._sleep(self._reconcile_interval)
            await self._reconcile_once()
