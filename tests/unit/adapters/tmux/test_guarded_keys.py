"""A key sequence sent only onto a screen that allows it, judged and typed under one lock hold.

`send_keys` checks nothing, and the callers that used to check first did it from a separate
capture, outside the key lock: another sender -- or the agent itself -- could change the screen
between the look and the keys. A stop's or a Remote Control toggle's `Enter` landing on a dialog
that arrived in that gap approves it (BL-055). `send_keys_when` is the relay's shape
(`deliver_prompt`, DEC-099) for a fixed sequence: one styled capture, the caller's predicate, and
the keys, all inside the session's `SessionKeyLock` (BL-056).
"""

from __future__ import annotations

import asyncio

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId
from remote_agents.ports.terminal import TerminalTargetMissing

from .support_targeting import pane_line


class _Pane:
    """One managed pane: a listing, one screen, and every call in order."""

    def __init__(
        self,
        screen: str = "screen",
        *,
        listed: bool = True,
        fail_on: str | None = None,
        capture_gate: asyncio.Event | None = None,
    ) -> None:
        self.session_id = SessionId.new()
        self._screen = screen
        self._listed = listed
        self._fail_on = fail_on
        self._capture_gate = capture_gate
        self.capturing = asyncio.Event()
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if self._fail_on is not None and self._fail_on in argv:
            raise RuntimeError("tmux command failed: can't find pane: %1")
        if "list-panes" in argv:
            return pane_line("%1", self.session_id, "2") if self._listed else ""
        if "capture-pane" in argv:
            self.capturing.set()
            if self._capture_gate is not None:
                await self._capture_gate.wait()
            return self._screen
        return ""

    async def feed(self, data: str, *argv: str) -> str:
        return await self.run(*argv)

    @property
    def keys(self) -> list[str]:
        return [call[-1] for call in self.calls if "send-keys" in call]

    @property
    def screen_reads_and_keys(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if {"capture-pane", "send-keys"} & set(call)]


def _gateway(pane: _Pane) -> TmuxGateway:
    return TmuxGateway("remote-agents-test-guarded-keys", pane)


def test_the_capture_is_styled_and_precedes_the_first_key() -> None:
    pane = _Pane()

    refused = asyncio.run(
        _gateway(pane).send_keys_when(pane.session_id, ("/exit", "Enter"), lambda _: True)
    )

    assert refused is None
    reads_and_keys = pane.screen_reads_and_keys
    assert "capture-pane" in reads_and_keys[0]
    assert "-e" in reads_and_keys[0], "the dim suggestion is only told from a draft when styled"
    assert [call[-1] for call in reads_and_keys[1:]] == ["/exit", "Enter"]


def test_the_predicate_judges_the_capture_it_was_given() -> None:
    pane = _Pane("the screen as tmux drew it")
    seen: list[str] = []

    asyncio.run(
        _gateway(pane).send_keys_when(
            pane.session_id, ("Enter",), lambda capture: seen.append(capture) or True
        )
    )

    assert seen == ["the screen as tmux drew it"]


def test_a_refusing_predicate_sends_nothing_and_returns_the_screen_it_refused() -> None:
    pane = _Pane("a dialog")

    refused = asyncio.run(
        _gateway(pane).send_keys_when(pane.session_id, ("/exit", "Enter"), lambda _: False)
    )

    assert refused == "a dialog"
    assert pane.keys == []


def test_the_lock_is_held_from_the_capture_through_the_last_key() -> None:
    """A second sender waiting on the lock types after the whole sequence, never inside it.

    The capture is held open, a plain `send_keys` is started for the same pane, and only then is
    the capture let go. If the lock were taken after the check -- the shape this replaces -- the
    second sender's key would land between the look and the keys it licensed.
    """

    async def scenario() -> list[str]:
        gate = asyncio.Event()
        pane = _Pane(capture_gate=gate)
        gateway = _gateway(pane)
        guarded = asyncio.create_task(
            gateway.send_keys_when(pane.session_id, ("/exit", "Enter"), lambda _: True)
        )
        await pane.capturing.wait()
        other = asyncio.create_task(gateway.send_keys(pane.session_id, ("x",)))
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(guarded, other)
        return pane.keys

    assert asyncio.run(scenario()) == ["/exit", "Enter", "x"]


@pytest.mark.parametrize(
    ("listed", "fail_on"),
    [(False, None), (True, "capture-pane"), (True, "send-keys")],
    ids=["not-listed", "capture-finds-no-pane", "keys-find-no-pane"],
)
def test_a_missing_pane_raises_target_missing_as_send_keys_does(
    listed: bool, fail_on: str | None
) -> None:
    pane = _Pane(listed=listed, fail_on=fail_on)

    with pytest.raises(TerminalTargetMissing):
        asyncio.run(_gateway(pane).send_keys_when(pane.session_id, ("Enter",), lambda _: True))


def test_an_empty_sequence_is_refused_as_send_keys_refuses_it() -> None:
    pane = _Pane()

    with pytest.raises(ValueError):
        asyncio.run(_gateway(pane).send_keys_when(pane.session_id, (), lambda _: True))
