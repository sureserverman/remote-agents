"""Push notification of state changes, as callback registration rather than iteration.

Callback-registration -- `subscribe(listener)` returning an `Unsubscribe` -- was chosen over
an async-iterator form because both current consumers are already callback-shaped: Textual's
message pump takes a posted message from any thread, and the bot's push path is a handler
invoked per event. An async iterator would have forced each of them to host a pump task whose
only job is turning `async for` back into the callback they wanted, while the reverse costs
nothing -- a future SSE/WebSocket adapter that genuinely streams can queue callbacks onto its
own send loop.

What a listener *receives* was deliberately undecided until 2026-09-12, on the grounds that
the first consumer to arrive should decide what a change notification must carry -- a bare
"something changed" ping, a session id, or a full snapshot.

**The first consumer arrived and chose the ping.** `application.store_watch.StoreWatch` learns
that the store moved by `stat`-ing its files, which is what DEC-035 leaves available to
something that is not performing an operation -- and file metadata genuinely cannot say *what*
changed. A record carrying a session id would be inventing one. So `StoreChanged` carries a
time and nothing else, and every subscriber re-reads, which is what they were going to do on
their timer anyway; the event only says when it is worth doing.

That is a floor rather than a ceiling. A later producer that does know more -- one reading the
write-ahead log, or the use case itself publishing as it writes -- adds a sibling record
beside this one; it does not widen this one into a field that is sometimes meaningful, which
is the shape `HostRemoteControlCommand` is a separate type to avoid.

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
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StoreChanged:
    """Something in the session store moved, at `at`. Deliberately says no more than that.

    Frozen because it is broadcast: one record reaches every listener in the process, and a
    mutable one would let the first consumer edit what the second receives.
    """

    at: datetime


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
