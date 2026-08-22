"""`run_private_bot` is driven for real, against a fake `ApplicationBuilder`.

This closes BL-006. The entry point had **no execution coverage at all**: the two tests
that drive `main(["serve", ...])` pass their own `serve_runner` and monkeypatch
`bootstrap._private_boundary`, so neither the real composition root nor this function's
body ever ran. Everything it does before the bot answers its first message -- registering
seven handlers, attaching the notifier to an application that does not exist until it is
built, refusing a competing webhook, and shutting down in the right order -- ran for the
first time in production, on the owner's phone.

That is not a theoretical gap. `2026-08-21-one-backend-two-frontends-sub-01` moved the
boundary's collaborators out of `__post_init__` and left this function's own
`boundary=None` default constructing a bare one, so `boundary.notifier.attach(...)` six
lines later would have raised `AttributeError` on a real run. A green suite of 2485 tests
said nothing, because nothing ran this code. It was found by a human reading the diff.

**The obstacle was always `ApplicationBuilder`**, which wants a bot token and a network.
It does not need injecting: it is imported at `service.py` module scope, so `monkeypatch`
replaces it and the function runs unmodified. No production code changed to make this
testable, which is the point -- a seam invented for a test is a seam the production path
does not use.

**What is faked, and what is real.** The fake is the Telegram *transport*: the builder, the
application, its bot and its updater. Everything else is the real thing -- the real
`build_private_bot`, the real handler classes from `python-telegram-bot`, the real
`_install_stop_signals`, the real ordering. The fake records an ordered lifecycle log, so
the assertions are about *sequence* as much as occurrence: attaching the notifier before
`initialize()` would leave it holding a bot that is not ready, and stopping the application
before its updater would drop updates already in flight.

Stated limit: no update is ever dispatched through the assembled application, so this
covers *wiring*, not the handlers' behaviour. That behaviour is covered -- the boundary is
driven end to end against a fake backend in `tests/e2e/test_telegram_fake_backend.py`. The
two halves meet at the handler list this file pins.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field
from typing import Any

import pytest
from backends import backend_for
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from remote_agents.adapters.telegram import service
from remote_agents.adapters.telegram.service import build_private_bot, run_private_bot
from remote_agents.config import TelegramSecrets

OWNER = 7
CHAT = 11
SECRETS = TelegramSecrets(bot_token="test-token", owner_user_id=OWNER, owner_chat_id=CHAT)


@dataclass
class _Webhook:
    url: str


@dataclass
class _FakeBot:
    log: list[str]
    webhook_url: str = ""
    commands_set: list[Any] = field(default_factory=list)

    async def delete_my_commands(self) -> None:
        self.log.append("bot.delete_my_commands")

    async def set_my_commands(self, commands: Any, scope: Any = None) -> None:
        self.log.append("bot.set_my_commands")
        self.commands_set = list(commands)

    async def set_chat_menu_button(self, chat_id: int, menu_button: Any) -> None:
        self.log.append("bot.set_chat_menu_button")

    async def set_my_description(self, description: str) -> None:
        self.log.append("bot.set_my_description")

    async def set_my_short_description(self, short_description: str) -> None:
        self.log.append("bot.set_my_short_description")

    async def get_webhook_info(self) -> _Webhook:
        self.log.append("bot.get_webhook_info")
        return _Webhook(self.webhook_url)


@dataclass
class _FakeUpdater:
    log: list[str]
    running: bool = False
    polled_with: dict[str, Any] = field(default_factory=dict)
    on_poll: Any = None

    async def start_polling(self, **kwargs: Any) -> None:
        self.log.append("updater.start_polling")
        self.polled_with = dict(kwargs)
        self.running = True
        if self.on_poll is not None:
            self.on_poll()

    async def stop(self) -> None:
        self.log.append("updater.stop")
        self.running = False


@dataclass
class _FakeApplication:
    log: list[str]
    bot: _FakeBot
    updater: _FakeUpdater | None
    running: bool = False
    handlers: list[Any] = field(default_factory=list)

    def add_handler(self, handler: Any) -> None:
        self.log.append("add_handler")
        self.handlers.append(handler)

    async def initialize(self) -> None:
        self.log.append("initialize")

    async def start(self) -> None:
        self.log.append("start")
        self.running = True

    async def stop(self) -> None:
        self.log.append("stop")
        self.running = False

    async def shutdown(self) -> None:
        self.log.append("shutdown")


class _FakeBuilder:
    """Records the chain, because `concurrent_updates(False)` is a correctness constraint."""

    def __init__(self, application: _FakeApplication, chain: dict[str, Any]) -> None:
        self._application = application
        self._chain = chain

    def token(self, token: str) -> _FakeBuilder:
        self._chain["token"] = token
        return self

    def concurrent_updates(self, value: bool) -> _FakeBuilder:
        self._chain["concurrent_updates"] = value
        return self

    def build(self) -> _FakeApplication:
        self._chain["built"] = True
        return self._application


@dataclass
class _Harness:
    log: list[str]
    chain: dict[str, Any]
    application: _FakeApplication
    bot: _FakeBot
    updater: _FakeUpdater | None
    stop_events: list[asyncio.Event]

    @property
    def attached(self) -> list[Any]:
        return [entry for entry in self.log if entry == "notifier.attach"]


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    webhook_url: str = "",
    with_updater: bool = True,
    stop_on_poll: bool = True,
) -> _Harness:
    """Replace the Telegram transport and let the real function run over it."""
    log: list[str] = []
    bot = _FakeBot(log, webhook_url=webhook_url)
    updater = _FakeUpdater(log) if with_updater else None
    application = _FakeApplication(log, bot=bot, updater=updater)
    chain: dict[str, Any] = {}

    monkeypatch.setattr(service, "ApplicationBuilder", lambda: _FakeBuilder(application, chain))

    # The stop event is a local of `run_private_bot`, so the test reaches it the same way the
    # process does -- through the installer. Capturing it here rather than raising a real
    # SIGTERM keeps the test from signalling the pytest process; the real installer is driven
    # separately by `test_the_stop_signals_set_the_event_they_are_given`.
    stop_events: list[asyncio.Event] = []

    def _capture(stopping: asyncio.Event) -> None:
        log.append("install_stop_signals")
        stop_events.append(stopping)

    monkeypatch.setattr(service, "_install_stop_signals", _capture)

    if updater is not None and stop_on_poll:
        # Setting the captured event is the only thing that ends `await stopping.wait()`, so a
        # test that finishes at all has proved the installer was handed the awaited event.
        updater.on_poll = lambda: stop_events[0].set()

    return _Harness(log, chain, application, bot, updater, stop_events)


def _boundary(log: list[str]) -> Any:
    boundary = build_private_bot(OWNER, CHAT, backend=backend_for())
    attach = boundary.notifier.attach

    def _record(bot: Any) -> None:
        log.append("notifier.attach")
        log.append(f"notifier.attach.bot={type(bot).__name__}")
        attach(bot)

    object.__setattr__(boundary.notifier, "attach", _record)
    return boundary


async def test_every_handler_the_owner_can_reach_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seven handlers, and nothing has ever checked that they are all there.

    A dropped or renamed handler is a command that silently stops existing: PTB simply has
    no route for the update, so the bot answers nothing and logs nothing. The count is
    pinned alongside the names for the reason DEC-041 gives -- an equality check on the set
    still passes when the source and the expectation grow together.
    """
    harness = _harness(monkeypatch)
    await run_private_bot(SECRETS, _boundary(harness.log))

    handlers = harness.application.handlers
    assert len(handlers) == 7, f"expected 7 handlers, found {len(handlers)}"

    commands = {
        next(iter(handler.commands)): handler.callback
        for handler in handlers
        if isinstance(handler, CommandHandler)
    }
    assert set(commands) == {"start", "launch", "resume", "sessions", "help"}

    callbacks = [h for h in handlers if isinstance(h, CallbackQueryHandler)]
    messages = [h for h in handlers if isinstance(h, MessageHandler)]
    assert len(callbacks) == 1, "the button handler is the whole navigation surface"
    assert len(messages) == 1, "the text handler is how a wizard step is answered"


async def test_each_command_routes_to_its_own_boundary_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names and callbacks together, because a right name on a wrong method is silent.

    `/launch` wired to `sessions_command` would register, route and answer -- with the
    wrong screen. Nothing downstream distinguishes that from a working bot.
    """
    harness = _harness(monkeypatch)
    boundary = _boundary(harness.log)
    await run_private_bot(SECRETS, boundary)

    routes = {
        next(iter(handler.commands)): handler.callback
        for handler in harness.application.handlers
        if isinstance(handler, CommandHandler)
    }
    assert routes == {
        "start": boundary.start,
        "launch": boundary.launch_command,
        "resume": boundary.resume_command,
        "sessions": boundary.sessions_command,
        "help": boundary.help_command,
    }

    callback = next(h for h in harness.application.handlers if isinstance(h, CallbackQueryHandler))
    message = next(h for h in harness.application.handlers if isinstance(h, MessageHandler))
    assert callback.callback == boundary.callback
    assert message.callback == boundary.text


async def test_the_notifier_is_attached_to_the_running_application_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one collaborator the factory cannot wire, and the near-miss BL-006 was raised for.

    Every other message answers an update and goes out through that update's own bot handle.
    A notification answers nothing, so it needs the application's -- and the application does
    not exist until `run_private_bot` builds it. Attaching before `initialize()` would hand
    the notifier a bot that is not ready, so the order is asserted, not just the call.
    """
    harness = _harness(monkeypatch)
    await run_private_bot(SECRETS, _boundary(harness.log))

    assert "notifier.attach" in harness.log, "the notifier was never attached; notifications die"
    assert harness.log.index("initialize") < harness.log.index("notifier.attach")
    assert "notifier.attach.bot=_FakeBot" in harness.log, "attached to something else"


async def test_a_configured_webhook_refuses_to_poll_and_still_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consumers on one bot is the failure; a leaked application is the second one."""
    harness = _harness(monkeypatch, webhook_url="https://example.invalid/hook")

    with pytest.raises(RuntimeError, match="refusing concurrent polling"):
        await run_private_bot(SECRETS, _boundary(harness.log))

    assert "updater.start_polling" not in harness.log, "it polled anyway"
    assert harness.log[-1] == "shutdown", "the refusal skipped the finally block"


async def test_a_missing_updater_refuses_rather_than_polling_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`application.updater` is `None` when PTB is built without one -- a silent no-op bot."""
    harness = _harness(monkeypatch, with_updater=False)

    with pytest.raises(RuntimeError, match="updater is unavailable"):
        await run_private_bot(SECRETS, _boundary(harness.log))

    assert harness.log[-1] == "shutdown"


async def test_it_polls_until_stopped_then_shuts_down_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shutdown order is the claim: updater first, then application, then shutdown.

    Stopping the application before its updater drops updates already in flight. Nothing
    checked the order, and nothing would have noticed it reversed.
    """
    harness = _harness(monkeypatch)
    await run_private_bot(SECRETS, _boundary(harness.log))

    assert harness.updater is not None
    assert harness.updater.polled_with == {"drop_pending_updates": False}, (
        "dropping pending updates on start loses commands sent while the service restarted"
    )

    tail = [entry for entry in harness.log if entry in {"updater.stop", "stop", "shutdown"}]
    assert tail == ["updater.stop", "stop", "shutdown"]
    assert harness.updater.running is False
    assert harness.application.running is False


async def test_the_owner_metadata_is_synced_before_the_bot_answers_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command menu the owner sees is written at startup, once, by this function."""
    harness = _harness(monkeypatch)
    await run_private_bot(SECRETS, _boundary(harness.log))

    for call in (
        "bot.delete_my_commands",
        "bot.set_my_commands",
        "bot.set_chat_menu_button",
        "bot.set_my_description",
        "bot.set_my_short_description",
    ):
        assert call in harness.log, f"{call} never ran; the owner's menu goes stale"
    assert harness.log.index("bot.set_my_commands") < harness.log.index("updater.start_polling")
    assert harness.bot.commands_set, "the owner's command list was set to nothing"


async def test_the_builder_is_driven_with_the_token_and_sequential_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same constraint `test_the_bot_handles_updates_sequentially` parses, now observed.

    That one reads the literal out of the source; this one watches the value arrive at the
    builder. Both are worth having: the parse fails on a non-literal the runtime check would
    silently accept, and the runtime check would catch a value rewritten between the literal
    and the call.
    """
    harness = _harness(monkeypatch)
    await run_private_bot(SECRETS, _boundary(harness.log))

    assert harness.chain["token"] == "test-token"
    assert harness.chain["concurrent_updates"] is False
    assert harness.chain["built"] is True


async def test_the_stop_signals_set_the_event_they_are_given() -> None:
    """The real installer, driven by a real signal, in isolation.

    `run_private_bot`'s own tests capture the event rather than raising SIGTERM, because a
    signal that escaped its handler would take the test runner with it. So the installer is
    proved here instead, on an event nobody else awaits.
    """
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        service._install_stop_signals(stopping)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(stopping.wait(), timeout=2)
        assert stopping.is_set()
    finally:
        for number in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(number)
            except (NotImplementedError, ValueError):
                pass
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)


async def test_the_default_boundary_is_a_wired_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The near-miss itself, pinned at the level it would actually have failed.

    `run_private_bot` carries `boundary: PrivateBotBoundary | None = None` and fills it
    itself. Sub-plan 1 moved the collaborators out of `__post_init__`, and this default went
    on constructing a bare `PrivateBotBoundary(...)` -- one with no `notifier` -- so
    `boundary.notifier.attach(...)` six lines later would have raised `AttributeError` on
    the first real start. Nothing ran it, so nothing said so.

    `test_the_factory_is_the_only_place_src_constructs_a_boundary` pins that statically, by
    parsing the tree for the wrong constructor call. This pins it dynamically: the default
    is taken, the function runs to the end, and the notifier it could not have had is
    attached. A static check answers "is the wrong call written anywhere"; this answers "does
    the default path work", and those come apart the moment someone writes a *different*
    wrong call.

    The boundary it builds carries an empty `Backend` -- no session or project use case --
    which is a host that answers "that is unavailable" rather than one that fails to start.
    That is the documented shape (`Backend`'s optional fields are a record of what a process
    wired), and starting is exactly what is being tested.
    """
    harness = _harness(monkeypatch)

    await run_private_bot(SECRETS)

    assert harness.log.count("add_handler") == 7, "the default path wired no handlers"
    assert harness.log[-1] == "shutdown", "the default path did not complete"
    assert "initialize" in harness.log
