"""What one frontend declares it needs — wiring and a claim, never use-case logic.

The frontend-side counterpart of `ports.provider_descriptor`: each surface exports one of
these from its package root so the composition root can read what it must wire instead of
discovering it on first use. `required_capabilities` names `Backend` fields the surface
cannot start without; composition validates the claim and fails loudly on a capability the
host wired `None` (DEC-043 — a shared rule is asked, not restated: the surface asks here,
once, rather than each screen re-checking).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FrontendDescriptor:
    """One frontend's identity, entry point, and capability claim."""

    name: str
    """The surface's short name, as commands and logs refer to it."""

    wire: object | None
    """The surface's wiring entry point — a callable deferring its own heavy imports — or
    None for a descriptor that only claims (tests fabricate these)."""

    required_capabilities: tuple[str, ...]
    """`Backend` field names this surface cannot start without. Validated at composition,
    where a false claim can still stop the process instead of a screen."""
