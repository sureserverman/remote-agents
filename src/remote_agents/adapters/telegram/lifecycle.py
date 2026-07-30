"""Manual python-telegram-bot composition plus a no-network recorded transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from telegram.ext import Application, ApplicationBuilder

from remote_agents.adapters.telegram.authorization import AuthorizationGate, AuthorizationUpdate


class TelegramRuntime(Protocol):
    async def initialize(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def shutdown(self) -> None: ...


class PollingTransport(Protocol):
    async def get_updates(self) -> tuple[RecordedUpdate, ...]: ...
    async def answer_callback(self, callback_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class RecordedUpdate:
    """Synthetic update data that intentionally omits Telegram usernames and messages."""

    token: str
    sender_id: int | None = None
    chat_id: int | None = None
    chat_type: str | None = None
    kind: str = "callback"
    callback_id: str | None = None


class FakeTelegramTransport:
    """Recorded update batches with no HTTP client or network-capable operation."""

    def __init__(
        self, batches: tuple[tuple[RecordedUpdate, ...], ...], *, failures: int = 0
    ) -> None:
        self._batches = list(batches)
        self._failures = failures
        self.acknowledged: list[str] = []
        self.network_calls = 0

    async def get_updates(self) -> tuple[RecordedUpdate, ...]:
        if self._failures:
            self._failures -= 1
            raise RuntimeError("synthetic_poll_failure")
        return self._batches.pop(0) if self._batches else ()

    async def answer_callback(self, callback_id: str) -> None:
        self.acknowledged.append(callback_id)


class TelegramLifecycle:
    """Own PTB's explicit initialize/start/stop/shutdown lifecycle without polling HTTP."""

    def __init__(self, runtime: TelegramRuntime) -> None:
        self._runtime = runtime
        self._stopped = asyncio.Event()

    async def run_forever(self) -> None:
        await self._runtime.initialize()
        await self._runtime.start()
        try:
            await self._stopped.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._runtime.stop()
            await self._runtime.shutdown()

    def request_stop(self) -> None:
        self._stopped.set()


class PollingAdapter:
    """Acknowledge callback queries and handle recorded tokens with bounded local retry."""

    def __init__(
        self,
        transport: PollingTransport,
        authorization: AuthorizationGate,
        handle: Callable[[str], None],
        *,
        retries: int,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if retries < 0:
            raise ValueError("retries cannot be negative")
        self._transport = transport
        self._authorization = authorization
        self._handle = handle
        self._retries = retries
        self._wait = wait

    async def poll_once(self) -> None:
        for attempt in range(self._retries + 1):
            try:
                updates = await self._transport.get_updates()
                break
            except RuntimeError:
                if attempt == self._retries:
                    raise
                await self._wait(0)
        for update in updates:
            if update.callback_id is not None:
                await self._transport.answer_callback(update.callback_id)
            self._authorization.dispatch(
                AuthorizationUpdate(
                    sender_id=update.sender_id,
                    chat_id=update.chat_id,
                    chat_type=update.chat_type,
                    kind=update.kind,
                ),
                lambda: self._handle(update.token),
            )


def build_ptb_application(token: str) -> Application:
    """Construct PTB without an updater; bootstrap owns any future polling task."""
    return ApplicationBuilder().token(token).updater(None).build()
