"""Push notification of state changes, as callback registration rather than iteration.

Callback-registration -- `subscribe(listener)` returning an `Unsubscribe` -- was chosen over
an async-iterator form because both current consumers are already callback-shaped: Textual's
message pump takes a posted message from any thread, and the bot's push path is a handler
invoked per event. An async iterator would have forced each of them to host a pump task whose
only job is turning `async for` back into the callback they wanted, while the reverse costs
nothing -- a future SSE/WebSocket adapter that genuinely streams can queue callbacks onto its
own send loop.

What a listener *receives* -- the `StateChange` vocabulary -- is deliberately undecided. No
consumer exists yet, and the first one to arrive decides what a change notification must
carry (a bare "something changed" ping, a session id, a full snapshot). Naming that record
here, ahead of the consumer, would be guessing, so the listener parameter stays loosely typed
until the first consumer pins it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

Unsubscribe = Callable[[], None]
"""Undoes one `subscribe` call: after it returns, that listener is never invoked again."""


@runtime_checkable
class StateEvents(Protocol):
    """A source of state-change notifications a caller can attach a listener to.

    `runtime_checkable` so a composition can ask `isinstance(obj, StateEvents)` when wiring
    an optional push path -- the same "absence is readable" posture the descriptors take.
    """

    def subscribe(self, listener: object) -> Unsubscribe:
        """Register `listener` for state changes; the return value detaches it.

        `listener` is deliberately `object`, not a typed callable -- see the module
        docstring: its call signature is the undecided `StateChange` vocabulary.
        """
        ...
