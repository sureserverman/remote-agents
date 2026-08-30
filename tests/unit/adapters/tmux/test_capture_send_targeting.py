"""Capture and key-sending address the agent's pane, and fall back rather than guess.

These two are where mis-targeting does its damage. A capture read from the wrong pane
feeds readiness detection, folder-trust classification and Remote Control state — every one
of which then decides something on evidence about somebody else's terminal. Pane-digest
quiet watching was a fourth consumer until it was retired on 2026-08-30; nothing reads a
capture for activity now. A keystroke sent to the wrong pane is worse, because it is not a
wrong reading but a wrong *write*: DEC-016 lets a surface answer the folder-trust question with
a bare Enter, which is the single most dangerous key this service sends.

So both resolve the pane first. A session that resolves to no pane — schema 1, or gone —
keeps the session target it has always used, which is what makes the upgrade continuous
rather than a cliff: an owner's running sessions do not stop working the moment this
ships.

`send_keys` resolves **once for the whole sequence**, not once per key. Fewer subprocess
round-trips, and a smaller window for the pane to move between the first key and the last
— a sequence that started addressing one pane must not finish addressing another.
"""

from __future__ import annotations

import pytest

from remote_agents.domain.models import SessionId
from remote_agents.ports.terminal import TerminalTargetMissing

from .support_targeting import TargetingRunner, gateway_for

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_SESSION_TARGET = "ra-01234567-89ab-cdef-0123-456789abcdef:"
_BASE = ("tmux", "-L", "remote-agents-test-target")


async def test_capture_addresses_the_resolved_pane() -> None:
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),), capture="hello")
    assert await gateway_for(runner).capture(_SESSION) == "hello"
    assert runner.capture_call == (*_BASE, "capture-pane", "-p", "-t", "%3")


async def test_pane_title_addresses_the_resolved_pane_without_a_capture() -> None:
    runner = TargetingRunner(
        panes=(("%3", _SESSION, "2"),), capture="[ ! ] Action Required | multitor"
    )

    assert await gateway_for(runner).pane_title(_SESSION) == "[ ! ] Action Required | multitor"

    assert runner.calls[-1] == (
        *_BASE,
        "display-message",
        "-p",
        "-t",
        "%3",
        "#{pane_title}",
    )
    assert not [call for call in runner.calls if "capture-pane" in call]


async def test_capture_addresses_the_pane_wherever_it_is_hosted() -> None:
    """The console holds the agent; the session target would read the surface instead."""
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),), host="ra-console", capture="agent")
    assert await gateway_for(runner).capture(_SESSION) == "agent"
    assert runner.capture_call == (*_BASE, "capture-pane", "-p", "-t", "%3")


async def test_capture_falls_back_to_the_session_target_for_a_legacy_session() -> None:
    runner = TargetingRunner(panes=(("%3", _SESSION, "1"),), capture="legacy")
    assert await gateway_for(runner).capture(_SESSION) == "legacy"
    assert runner.capture_call == (*_BASE, "capture-pane", "-p", "-t", _SESSION_TARGET)


async def test_nothing_resolving_at_all_refuses_rather_than_falling_back() -> None:
    """No pane claims this identity, so there is no target — not a session target either.

    Two situations produce this, and refusing is right for both. The session is gone, in
    which case any target errors and the caller gets the same typed answer one call earlier.
    Or the session exists and something foreign occupies its window — a displaced legacy
    pane's replacement — in which case falling back reads and types at a stranger. The
    fallback's precondition is that the session's own window still holds the pane claiming
    the identity, and it is checked rather than assumed.
    """
    runner = TargetingRunner(panes=(), capture="somebody else's screen")
    with pytest.raises(TerminalTargetMissing):
        await gateway_for(runner).capture(_SESSION)
    assert not [call for call in runner.calls if "capture-pane" in call]


async def test_a_displaced_legacy_pane_is_refused_rather_than_mis_targeted() -> None:
    """The case that motivated checking the precondition, reproduced at unit scale.

    A schema-1 mark under a foreign host is inheritance rather than identity, so the pane
    does not decode and nothing resolves. What is left in the home window is whatever swapped
    in. Measured live by the close-out evaluator: with the old fallback, `capture` returned a
    stranger's screen and `send_keys` landed in their terminal.
    """
    displaced = TargetingRunner(panes=(("%3", _SESSION, "1"),), host="ra-console")
    with pytest.raises(TerminalTargetMissing):
        await gateway_for(displaced).send_keys(_SESSION, ("Enter",))
    assert displaced.key_calls == []


async def test_send_keys_addresses_the_resolved_pane_for_every_key() -> None:
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),))
    await gateway_for(runner).send_keys(_SESSION, ("/remote-control", "Enter"))
    assert runner.key_calls == [
        (*_BASE, "send-keys", "-t", "%3", "/remote-control"),
        (*_BASE, "send-keys", "-t", "%3", "Enter"),
    ]


async def test_send_keys_resolves_once_for_the_whole_sequence() -> None:
    """Two keys, one resolution — fewer round-trips, and no chance of the sequence
    starting at one pane and finishing at another."""
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),))
    await gateway_for(runner).send_keys(_SESSION, ("/remote-control", "Enter"))
    assert runner.listings == 1


async def test_send_keys_falls_back_to_the_session_target_for_a_legacy_session() -> None:
    runner = TargetingRunner(panes=(("%3", _SESSION, "1"),))
    await gateway_for(runner).send_keys(_SESSION, ("Enter",))
    assert runner.key_calls == [(*_BASE, "send-keys", "-t", _SESSION_TARGET, "Enter")]


async def test_an_empty_key_sequence_is_still_refused_before_anything_is_resolved() -> None:
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),))
    with pytest.raises(ValueError):
        await gateway_for(runner).send_keys(_SESSION, ())
    assert runner.listings == 0


async def test_a_gone_pane_still_raises_the_typed_missing_error() -> None:
    """Retyping is what lets a caller answer "already gone" instead of propagating a
    failure it cannot read — unchanged by the move to pane targets."""
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),), fail_with="can't find pane")
    with pytest.raises(TerminalTargetMissing):
        await gateway_for(runner).capture(_SESSION)


async def test_a_failed_resolution_refuses_to_act_rather_than_falling_back() -> None:
    """The asymmetric failure, which is the dangerous one and the reason this does not fall
    back: the **listing** fails while a single-target call against the vacated window would
    have succeeded perfectly — against whatever pane is there now.

    `ra-<uuid>:` is a *window* target, so once the console exchanges panes it names the
    occupant rather than the agent. Falling back on a failed resolution would therefore read
    somebody else's screen, and on the `send_keys` path type into their terminal — with
    DEC-016 putting a bare Enter on exactly that path. So the error is raised, and the
    assertion that matters is that **no operation was issued at all**.
    """
    runner = TargetingRunner(
        panes=(("%3", _SESSION, "2"),),
        listing_fails="tmux command failed: can't find session: ra-x",
        capture="this is somebody else's screen",
    )
    with pytest.raises(TerminalTargetMissing):
        await gateway_for(runner).capture(_SESSION)
    assert not [call for call in runner.calls if "capture-pane" in call]


async def test_a_failed_resolution_refuses_to_type_as_well_as_to_read() -> None:
    runner = TargetingRunner(
        panes=(("%3", _SESSION, "2"),),
        listing_fails="tmux command failed: can't find session: ra-x",
    )
    with pytest.raises(TerminalTargetMissing):
        await gateway_for(runner).send_keys(_SESSION, ("Enter",))
    assert runner.key_calls == []


async def test_a_broken_tmux_is_still_never_mistaken_for_an_ended_session() -> None:
    """Refusing to fall back must not turn every listing failure into "already gone" —
    a tmux that is merely broken still arrives as itself."""
    runner = TargetingRunner(
        panes=(("%3", _SESSION, "2"),),
        listing_fails="tmux command failed: usage: list-panes [-as]",
    )
    with pytest.raises(RuntimeError) as raised:
        await gateway_for(runner).capture(_SESSION)
    assert not isinstance(raised.value, TerminalTargetMissing)


async def test_a_pane_that_vanishes_mid_sequence_is_typed_evidence_not_an_opaque_failure() -> None:
    """DEC-022 turns on telling a stop that was never sent from one that was, so the keys
    loop retypes like every other single-target operation rather than raising raw."""
    runner = TargetingRunner(
        panes=(("%3", _SESSION, "2"),), fail_with="tmux command failed: can't find pane: %3"
    )
    with pytest.raises(TerminalTargetMissing):
        await gateway_for(runner).send_keys(_SESSION, ("/quit", "Enter"))
