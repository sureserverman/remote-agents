"""What a provider boundary raises when it cannot answer, named where both sides may see it.

`application/` may not import an adapter (ARCH-02), so an application service that wants to
turn "the provider would not answer" into a rendered reading could not name the exception it
needed to catch: `adapters.agents.protocols.ProtocolError` is on the wrong side of the line.
The alternatives were both worse -- catching `Exception` at a use case, which swallows the
bugs this project wants loud, or duplicating the type, which makes two exceptions that mean
one thing and an `except` clause that catches the wrong one.

So the *meaning* lives here, in `ports/`, which both layers may import, and the adapters'
existing `ProtocolError` becomes a subclass of it. Nothing that raises or catches
`ProtocolError` today changes behaviour; the application simply gains a name for the
category that does not reach across the boundary to get it.
"""

from __future__ import annotations


class ProviderUnavailable(RuntimeError):
    """A provider's boundary is unreachable, or answered something it cannot mean.

    Deliberately one category rather than a hierarchy. Every caller so far does the same
    thing with it -- renders an honest "cannot say" -- and a distinction no caller acts on
    is a distinction that goes stale.
    """
