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

Registration and teardown are synchronous, and that is a decided contract rather than an
omission: `subscribe` records a callable and `Unsubscribe` forgets it -- neither performs
I/O, so neither needs an event loop. An async-native consumer registers a listener that
*schedules* onto its own loop (`call_soon_threadsafe`, `create_task`) rather than awaiting
inside the source, and an adapter whose teardown genuinely is async (an SSE connection to
close) owns that work behind the returned callable -- the callable detaches the listener
synchronously and may schedule the rest. Pinned now, while no consumer exists to break,
because retrofitting `async def subscribe` after first consumption is the breaking change
this port exists to avoid.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

Unsubscribe = Callable[[], None]
"""Undoes one `subscribe` call: after it returns, that listener is never invoked again.

Idempotent -- a second call is a no-op, never an error -- so a consumer tearing down on an
error path may call it without tracking whether it already did."""


@runtime_checkable
class StateEvents(Protocol):
    """A source of state-change notifications a caller can attach a listener to.

    `runtime_checkable` so a composition can ask `isinstance(obj, StateEvents)` when wiring
    an optional push path -- the same "absence is readable" posture the descriptors take.
    (`isinstance` checks method presence only, never the signature.)
    """

    def subscribe(self, listener: object) -> Unsubscribe:
        """Register `listener` for state changes; the return value detaches it.

        `listener` is deliberately `object`, not a typed callable -- see the module
        docstring: its call signature is the undecided `StateChange` vocabulary.
        """
        ...
