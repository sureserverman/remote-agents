"""The Codex pairing code is rendered once and reaches nothing else.

A manual pairing code lets whoever holds it attach a phone to this machine's Codex daemon
until it expires. It is the sharpest instance of DEC-013 -- what a provider hands this
service is rendered, never stored -- because the ways a secret escapes are all the ways
nobody chose: a DEBUG log record written for an unrelated reason, an exception whose message
interpolates the object, a `repr` in a traceback frame a crash reporter walks.

So the test is written as an *absence over everything observable*, not as a check of the two
call sites that exist today. It captures logging at DEBUG across the whole logger tree,
drives the real adapter, and then asserts the string is in none of it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from remote_agents.adapters.agents.codex.remote_control import (
    REMOTE_CONTROL_ARGV,
    CodexRemoteControl,
    CommandResult,
)
from remote_agents.adapters.agents.protocols import ProtocolError
from remote_agents.domain.remote_control import PairingCode

#: The secret this test hunts for. Distinctive enough that a substring match cannot be a
#: coincidence, and shaped like a real manual pairing code.
SECRET = "ZZZZ-9999"

EXPIRES = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

#: The one owner this bot answers, as every other Telegram test spells it.
_OWNER = 4242
_CHAT = 909

#: The spelling the real CLI prints, verified from `codex app-server generate-ts
#: --experimental` against the installed codex-cli 0.151.0: camelCase throughout.
PAIR_RESPONSE = json.dumps(
    {
        "pairingCode": "0000-0000",
        "manualPairingCode": SECRET,
        "environmentId": "env_test",
        "expiresAt": 1788436800,
    }
)


@dataclass
class RecordingRunner:
    """A runner that answers `pair` with the secret and records nothing else."""

    result: CommandResult
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        self.calls.append(argv)
        return self.result


def _minted(stdout: str = PAIR_RESPONSE, returncode: int = 0) -> CodexRemoteControl:
    runner = RecordingRunner(CommandResult(returncode=returncode, stdout=stdout, stderr=""))
    return CodexRemoteControl(runner=runner, rpc=object())  # type: ignore[arg-type]


async def test_minting_a_pairing_code_writes_it_to_no_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every logger, at DEBUG -- the level nobody remembers is on in development."""
    with caplog.at_level(logging.DEBUG):
        code = await _minted().pair()

    assert code.code == SECRET, "a test that never obtained the secret proves nothing"
    for record in caplog.records:
        assert SECRET not in record.getMessage(), record.name
        assert SECRET not in str(record.args or ""), record.name
    assert SECRET not in caplog.text


async def test_the_minted_object_does_not_carry_the_code_in_its_repr() -> None:
    """A `repr` reaches tracebacks and debugger dumps nobody decided to write it into."""
    code = await _minted().pair()

    assert isinstance(code, PairingCode)
    assert SECRET not in repr(code)
    assert SECRET not in str(code)
    assert SECRET not in f"{code}"
    assert SECRET not in "{}".format(code)  # noqa: UP032 -- the point is the format protocol


async def test_a_failed_mint_puts_nothing_of_what_codex_printed_in_the_exception() -> None:
    """The failure path is the one that interpolates, because failures want context."""
    runner = RecordingRunner(
        CommandResult(returncode=1, stdout=PAIR_RESPONSE, stderr=f"rejected code {SECRET}")
    )
    subject = CodexRemoteControl(runner=runner, rpc=object())  # type: ignore[arg-type]

    with pytest.raises(ProtocolError) as raised:
        await subject.pair()

    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


async def test_a_malformed_mint_response_is_refused_without_quoting_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A response carrying the secret under an unexpected key must not be echoed back."""
    stdout = json.dumps({"pairingCode": SECRET, "expiresAt": 1788436800})

    with caplog.at_level(logging.DEBUG), pytest.raises(ProtocolError) as raised:
        await _minted(stdout=stdout).pair()

    assert SECRET not in str(raised.value)
    assert SECRET not in caplog.text


def test_the_pairing_argv_is_the_only_command_that_can_mint_one() -> None:
    """A second minting path would need a second argv; there is exactly one."""
    minting = [name for name, argv in REMOTE_CONTROL_ARGV.items() if "pair" in argv]
    assert minting == ["pair"], minting


# --- The two surfaces ------------------------------------------------------------------
#
# Everything above proves the adapter and the domain type keep the secret. These prove the
# two places it is deliberately *rendered* do not also keep it -- which is the harder claim,
# because a surface's job is to put the value on a screen.


def _surface_backend(code: str = SECRET, *, for_terminal: bool = False):
    """A backend whose host toggle mints `code`, with a store that records every write.

    `TuiContext` refuses a backend missing `sessions`, so the terminal case is handed a
    stand-in for the one field it checks -- the surface under test here is the pairing
    path, not the session list.
    """
    from backends import FakeHostRemoteControl, SessionUseCaseDouble, backend_for

    from remote_agents.domain.remote_control import HostConnection

    control = FakeHostRemoteControl(HostConnection.CONNECTED)
    control.pairing_code = code
    extra: dict[str, object] = {}
    if for_terminal:
        extra = {
            "sessions": SessionUseCaseDouble(),
            "projects": object(),
            "refresh_catalogue": tuple,
        }
    return control, backend_for(host_remote_control=control, **extra)


async def test_the_bot_renders_the_code_and_keeps_none_of_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The message carries it; the process must not."""
    from remote_agents.adapters.telegram.service import build_private_bot

    control, backend = _surface_backend()
    bot = build_private_bot(_OWNER, _CHAT, backend=backend)

    with caplog.at_level(logging.DEBUG):
        screen = await bot._host_remote_control_reply()
        token = next(
            button.callback_data
            for row in screen.keyboard
            for button in row
            if "Pair" in button.text
        )
        bot.callbacks.bind_pending(_CHAT, 1)
        reply = await bot._host_pair_reply(token, 1)

    assert SECRET in reply["text"], "a test that never rendered the secret proves nothing"
    assert control.calls.count("pair") == 1
    # Everything that is not the one message.
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage(), record.name
    assert SECRET not in repr(bot.callbacks)
    assert SECRET not in repr(backend.host_remote_control)


async def test_the_bot_puts_no_control_under_the_secret_that_could_re_send_it() -> None:
    """A keyboard is the one affordance that turns a shown-once message into shown-again."""
    from remote_agents.adapters.telegram.service import build_private_bot

    _, backend = _surface_backend()
    bot = build_private_bot(_OWNER, _CHAT, backend=backend)
    screen = await bot._host_remote_control_reply()
    token = next(
        button.callback_data for row in screen.keyboard for button in row if "Pair" in button.text
    )
    bot.callbacks.bind_pending(_CHAT, 1)
    reply = await bot._host_pair_reply(token, 1)

    assert not reply["reply_markup"].inline_keyboard


def test_no_persistence_path_names_a_pairing_code() -> None:
    """Structural, over the whole store surface rather than over the writes one test made.

    A sweep, because "the fake store recorded no write" only proves the path this test took.
    The store's own modules are where a value would have to be named to be persisted at all.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents"
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in sorted((root / "adapters" / "sqlite").rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "pairing" in line.lower() or "PairingCode" in line
    ]
    offenders += [
        f"ports/session_store.py:{number}"
        for number, line in enumerate(
            (root / "ports" / "session_store.py").read_text(encoding="utf-8").splitlines(), 1
        )
        if "pairing" in line.lower() or "PairingCode" in line
    ]
    assert not offenders, f"a persistence module names the pairing code: {offenders}"


def test_the_activity_table_and_the_capture_path_cannot_carry_one() -> None:
    """The two other places provider text reaches durable storage in this project."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents"
    for relative in ("agent_event.py", "application/captures.py", "application/activity.py"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "PairingCode" not in text, relative
        assert "pairing" not in text.lower(), relative


def test_the_terminal_modal_holds_the_code_and_publishes_no_way_back_to_it() -> None:
    """Dismissed is gone: no snapshot baseline, no position, no re-open."""
    from remote_agents.adapters.tui.screens.confirm import ALL_CONFIRMS, HostPairingCodeModal
    from remote_agents.domain.remote_control import PairingCode

    modal = HostPairingCodeModal(PairingCode(code=SECRET, expires_at=EXPIRES))

    assert SECRET in modal.rendered_code(), "a modal that never showed it proves nothing"
    assert SECRET not in repr(modal)
    assert getattr(HostPairingCodeModal, "position", "") == "", (
        "a snapshot baseline of this modal would be a secret in the repository"
    )
    assert HostPairingCodeModal not in ALL_CONFIRMS


def test_no_snapshot_baseline_contains_a_pairing_code() -> None:
    """The committed pictures of this surface, swept for the shape of a code.

    Written as a sweep of the baselines themselves rather than as a claim about which screens
    are registered, because the failure it guards against is a *file* -- and a file is what
    gets committed.
    """
    import pathlib
    import re

    baselines = pathlib.Path(__file__).resolve().parents[1] / "unit/adapters/tui/snapshots"
    # Bounded on both sides against `-` and word characters, so a UUID's inner groups do not
    # match: `deadbeef-0000-0000-...` is a session id the dashboards legitimately draw, and a
    # sweep that flags it is a sweep nobody keeps running.
    pattern = re.compile(r"(?<![\w-])[A-Z0-9]{4}-[A-Z0-9]{4}(?![\w-])")
    offenders = [
        f"{path.name}: {pattern.search(path.read_text(encoding='utf-8')).group()}"
        for path in sorted(baselines.glob("*.svg"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"a snapshot baseline holds something code-shaped: {offenders}"


# --- The failure path, which is where the logging actually happens -----------------------
#
# The success-path assertions above are green whether or not the failure path leaks, because
# the failure path is the only one that logs anything at all. That gap was real: both
# surfaces called `_LOG.exception`, which attaches `exc_info`, and a formatted record ends
# in `str(exception)` -- so an exception carrying the code put it in the log while
# `record.getMessage()` stayed clean. These drive that path.


async def test_a_failed_mint_puts_nothing_in_the_bot_s_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from remote_agents.adapters.telegram.service import build_private_bot

    control, backend = _surface_backend()
    control.fail_with = RuntimeError(f"relay refused code {SECRET}")
    bot = build_private_bot(_OWNER, _CHAT, backend=backend)

    screen = await bot._host_remote_control_reply()
    token = next(
        button.callback_data for row in screen.keyboard for button in row if "Pair" in button.text
    )
    with caplog.at_level(logging.DEBUG):
        bot.callbacks.bind_pending(_CHAT, 1)
        reply = await bot._host_pair_reply(token, 1)

    assert "no" in reply["text"].lower() and "pairing code" in reply["text"].lower(), (
        "the failure path was not actually driven"
    )
    # It must NOT claim nothing happened: `pair` can fail after the relay minted a live code
    # this process never rendered and cannot revoke, so "nothing changed" is the one sentence
    # an owner could act wrongly on.
    assert "expires on its own" in reply["text"]
    assert SECRET not in reply["text"]
    # `caplog.text` is the FORMATTED output, traceback included -- which is the whole point.
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage(), record.name


async def test_a_failed_mint_puts_nothing_in_the_terminal_s_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    control, backend = _surface_backend(for_terminal=True)
    control.fail_with = RuntimeError(f"relay refused code {SECRET}")
    app = RemoteAgentsTui(
        TuiContext(
            backend=backend,
            profiles=(ProfileAvailability("codex", True),),
            attach_argv=lambda session_id: ("tmux",),
        )
    )

    class _Screen:
        def announce(self, message: str, **kwargs: object) -> None:
            self.said = message

        def awaiting(self, _message: str):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _nothing():
                yield

            return _nothing()

    screen = _Screen()
    with caplog.at_level(logging.DEBUG):
        await app.pair_host_remote_control(screen)  # type: ignore[arg-type]

    assert getattr(screen, "said", ""), "the failure path was not actually driven"
    assert SECRET not in screen.said
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage(), record.name


async def test_the_bot_will_not_mint_against_a_reading_that_has_gone_stale() -> None:
    """The button was drawn a screen ago; the link may have dropped since.

    The terminal re-reads before minting and the bot did not, so a press against a stale
    CONNECTED screen would mint a live code on a host the policy says may not be paired.
    """
    from remote_agents.adapters.telegram.service import build_private_bot
    from remote_agents.domain.remote_control import HostConnection

    control, backend = _surface_backend()
    bot = build_private_bot(_OWNER, _CHAT, backend=backend)
    screen = await bot._host_remote_control_reply()
    token = next(
        button.callback_data for row in screen.keyboard for button in row if "Pair" in button.text
    )

    control.connection = HostConnection.DISABLED  # the link drops before the press lands

    bot.callbacks.bind_pending(_CHAT, 1)
    reply = await bot._host_pair_reply(token, 1)

    assert control.calls.count("pair") == 0, "a stale press minted a live code"
    assert SECRET not in reply["text"]


def test_a_code_carrying_markup_cannot_crash_the_screen_that_shows_it() -> None:
    """`[` is printable, so the upstream validator admits it; Textual's markup would not."""
    from remote_agents.adapters.tui.screens.confirm import HostPairingCodeModal
    from remote_agents.domain.remote_control import PairingCode

    modal = HostPairingCodeModal(PairingCode(code="[bold]ZZZZ", expires_at=EXPIRES))
    widget = next(iter(modal.compose()))

    assert getattr(widget, "_render_markup", True) is False or widget._content_type != "markup", (
        "the one screen whose job is to show provider text renders it as markup"
    )


# --- The one the "no keyboard" assertion could not see -----------------------------------


async def test_the_live_view_never_re_sends_the_pairing_message_by_itself() -> None:
    """`move_to_bottom` re-sends the last remembered screen with no press involved.

    It runs once per activity-notification pass, to keep the menu reachable below whatever
    notifications have arrived since. That is right for a menu and catastrophic for a
    secret: the pairing reply was remembered like any other screen, so the next pass sent the
    code again as a brand new message with its own push notification, under a line that said
    "shown once" -- and again on every pass after that, for as long as it stayed the live
    view. Reproduced end to end before the fix.

    The existing assertion that the message carries no keyboard could not see this. It tests
    the mechanism the author had in mind; the re-send path was never the keyboard.
    """
    from remote_agents.adapters.telegram.live_view import LiveView
    from remote_agents.adapters.telegram.service import (
        _UNREMEMBERED_ACTIONS,
        build_private_bot,
    )

    class _Anchors:
        def __init__(self) -> None:
            self._anchor: int | None = 100

        def anchor(self, _chat: int) -> int | None:
            return self._anchor

        def record_anchor(self, _chat: int, message_id: int) -> None:
            self._anchor = message_id

        def clear_anchor(self, _chat: int) -> None:
            self._anchor = None

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def edit_message_text(self, **_kw: object) -> None:
            return None

        async def send_message(self, **kw: object):
            self.sent.append(kw)

            class _Message:
                message_id = 200 + len(self.sent)

            return _Message()

        async def delete_message(self, **_kw: object) -> None:
            return None

    control, backend = _surface_backend()
    boundary = build_private_bot(_OWNER, _CHAT, backend=backend)
    screen = await boundary._host_remote_control_reply()
    token = next(
        button.callback_data for row in screen.keyboard for button in row if "Pair" in button.text
    )
    boundary.callbacks.bind_pending(_CHAT, 1)
    reply = await boundary._host_pair_reply(token, 1)
    assert SECRET in reply["text"], "a test that never rendered the secret proves nothing"

    view = LiveView(chat_id=_CHAT, anchors=_Anchors(), callbacks=boundary.callbacks)
    bot = _Bot()
    # Driven through the SERVICE's own decision, not by handing `LiveView` the flag
    # directly. An earlier version of this test passed `remember=False` itself, so
    # deleting the service's choice to pass it left every assertion green -- the test
    # proved `LiveView` could keep a secret while the code that must ask it to stopped
    # asking.
    boundary.view = view

    class _Query:
        def get_bot(self):
            return bot

    await boundary._render(
        _Query(), reply, remember="host.remote.pair" not in _UNREMEMBERED_ACTIONS
    )
    await view.move_to_bottom(bot)  # one activity-notification pass

    leaked = [message for message in bot.sent if SECRET in str(message.get("text", ""))]
    assert not leaked, (
        f"the live view re-sent the pairing code with no press: {len(leaked)} message(s)"
    )


async def test_a_remembered_screen_is_still_re_sent_so_the_guard_is_not_vacuous() -> None:
    """The mechanism still works for the screens it exists for -- menus, not secrets."""
    from remote_agents.adapters.telegram.live_view import LiveView
    from remote_agents.adapters.telegram.service import build_private_bot

    class _Anchors:
        def anchor(self, _chat: int) -> int:
            return 100

        def record_anchor(self, _chat: int, _message_id: int) -> None:
            return None

        def clear_anchor(self, _chat: int) -> None:
            return None

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def edit_message_text(self, **_kw: object) -> None:
            return None

        async def send_message(self, **kw: object):
            self.sent.append(kw)

            class _Message:
                message_id = 201

            return _Message()

        async def delete_message(self, **_kw: object) -> None:
            return None

    _, backend = _surface_backend()
    boundary = build_private_bot(_OWNER, _CHAT, backend=backend)
    ordinary = await boundary._host_remote_control_reply()
    view = LiveView(chat_id=_CHAT, anchors=_Anchors(), callbacks=boundary.callbacks)
    bot = _Bot()

    from remote_agents.adapters.telegram.service import _reply_arguments

    await view.render(bot, _reply_arguments(ordinary), retire=True)
    await view.move_to_bottom(bot)

    assert bot.sent, "move_to_bottom stopped working for the screens it is for"


def test_the_pairing_action_is_the_one_the_live_view_must_not_remember() -> None:
    """Named in one place, so a second secret-bearing screen is a deliberate addition."""
    from remote_agents.adapters.telegram.service import _UNREMEMBERED_ACTIONS

    assert _UNREMEMBERED_ACTIONS == frozenset({"host.remote.pair"})
