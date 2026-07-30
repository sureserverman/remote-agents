"""Manual Telegram lifecycle stays local when driven by the fake transport."""

import asyncio

from remote_agents.adapters.telegram.authorization import AuthorizationGate, ContentFreeDenialLog
from remote_agents.adapters.telegram.lifecycle import (
    FakeTelegramTransport,
    PollingAdapter,
    RecordedUpdate,
    TelegramLifecycle,
    build_ptb_application,
)


class FakeRuntime:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def initialize(self) -> None:
        self.events.append("initialize")

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")

    async def shutdown(self) -> None:
        self.events.append("shutdown")


async def test_manual_lifecycle_orders_shutdown_after_polling_cancellation() -> None:
    runtime = FakeRuntime()
    adapter = TelegramLifecycle(runtime)

    task = asyncio.create_task(adapter.run_forever())
    await asyncio.sleep(0)
    task.cancel()
    await task

    assert runtime.events == ["initialize", "start", "stop", "shutdown"]


async def test_fake_transport_acknowledges_callbacks_and_retries_bounded_poll_failures() -> None:
    transport = FakeTelegramTransport(
        (
            (
                RecordedUpdate(
                    "callback-1",
                    sender_id=7,
                    chat_id=11,
                    chat_type="private",
                    callback_id="answer-1",
                ),
            ),
        ),
        failures=1,
    )
    handled: list[str] = []
    adapter = PollingAdapter(
        transport,
        AuthorizationGate(7, 11, ContentFreeDenialLog()),
        handled.append,
        retries=1,
        wait=lambda _delay: _ready(),
    )

    await adapter.poll_once()

    assert handled == ["callback-1"]
    assert transport.acknowledged == ["answer-1"]
    assert transport.network_calls == 0


async def test_polling_adapter_authorizes_each_update_before_invoking_the_handler() -> None:
    transport = FakeTelegramTransport(
        (
            (
                RecordedUpdate("trusted", sender_id=7, chat_id=11, chat_type="private"),
                RecordedUpdate("untrusted", sender_id=8, chat_id=11, chat_type="private"),
            ),
        )
    )
    handled: list[str] = []
    denials = ContentFreeDenialLog()
    adapter = PollingAdapter(
        transport,
        AuthorizationGate(7, 11, denials),
        handled.append,
        retries=0,
        wait=lambda _delay: _ready(),
    )

    await adapter.poll_once()

    assert handled == ["trusted"]
    assert denials.events == ("denied",)


def test_ptb_composition_can_be_built_without_starting_an_updater() -> None:
    application = build_ptb_application("123456:synthetic-token")

    assert application.updater is None


async def _ready() -> None:
    return None
