"""A stand-in for the console's published selection, for surfaces that read or write it.

The two capabilities are plain callables on `TuiContext` (DEC-046: the surface is handed what
it may do, never a handle it probes), so a double for them is a recorder rather than a fake
tmux. Shared here rather than rewritten per test file for the reason the vocabulary tests give
about option names: three test modules writing their own recorder is three chances to disagree
about what "no selection" is, and the answer — `None`, never a sentinel string — is exactly the
thing the production decoder exists to guarantee.

`published` is a *list*, not a last-value, and that is deliberate. The sessions pane publishes
on every highlight change, on the vanished-row branch, and on unmount; a double that kept only
the latest could not tell "published once, correctly" from "published four times, the last of
which happened to be right", and the second is a pane writing on a path nobody meant it to.
"""

from __future__ import annotations

import asyncio

from remote_agents.domain.models import SessionId


class SelectionConsole:
    """Records what a surface publishes, and serves whatever the test says is selected."""

    def __init__(self, selected: SessionId | None = None, *, holds_slot: bool = True) -> None:
        #: Every publication in order, including the `None`s. See the module docstring.
        self.published: list[SessionId | None] = []
        #: What `read` answers. Settable mid-test, because the interesting cases are the ones
        #: where the selection changes underneath a pane that is about to act on it.
        self.selected = selected
        #: Per-call publication delays, in issue order. See `publish`.
        self.delays: list[float] = []
        #: How many times the surface read. Pinned by the tests that assert a chord re-reads
        #: rather than caching: one read per press is the price of never being stale, and a
        #: cache would be invisible in any assertion about the *value*.
        self.reads = 0
        #: Whether the pane this surface runs in is one of the console's own, by its slot mark
        #: and by where the mark currently is. `True` is the ordinary console pane; `False` is
        #: both cases the Stage 3 read-gate exists for -- a plain `remote-agents tui` on the
        #: console's socket, and the projects pane after a DEC-040 exchange parks it in an
        #: agent's window. Settable mid-test, because the second of those changes underneath a
        #: process that is already running.
        self.holds_slot = holds_slot
        #: How many times the gate was asked. Pinned for the same reason as `reads`: the answer
        #: has to be taken per press, since an exchange moves the pane while the process lives.
        self.slot_reads = 0

    async def publish(self, session_id: SessionId | None) -> None:
        """Record a publication, after whatever delay `delays` prescribes for it.

        The delay exists because without one this double **cannot fail**. Its body has no
        suspension point, so every publication scheduled onto the event loop completes in the
        order it was issued, whatever the code under test does — and a test asserting "the last
        write wins" then passes against an implementation that races, because the double is
        incapable of racing. Found by a Tier-2 review, which named it as the same
        "X does not happen" vacuity one level removed.

        `delays` is popped per call, so a test can make the *first* publication the slow one and
        watch what the implementation does with the second.
        """
        delay = self.delays.pop(0) if self.delays else 0.0
        if delay:
            await asyncio.sleep(delay)
        self.published.append(session_id)

    async def read(self) -> SessionId | None:
        self.reads += 1
        return self.selected

    async def holds_console_slot(self) -> bool:
        self.slot_reads += 1
        return self.holds_slot
