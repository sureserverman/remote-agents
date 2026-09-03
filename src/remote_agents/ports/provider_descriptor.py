"""What one provider declares it can do — capabilities as fields, absence as None.

A descriptor is the provider-side counterpart of `application.backend.Backend`: the sealed
record of what one curated agent's integration wired, read by callers as declared fields
rather than discovered by probing. Every capability field is `<something> | None`, and the
`None` is a statement, not a gap (DEC-061): the providers genuinely disagree about what they
publish — Cursor reports no usage at all, only Claude and Codex take hooks — so a host that
wired nothing for a capability says so in a way a frontend can read with `is None` and
render honestly, never invent.

The capability fields are loosely typed for the reason `Backend`'s are: naming the concrete
reader and installer types here would pull adapter modules into the ports layer, which
`tests/architecture/check_imports.py` forbids — ports may import only domain and ports. What
actually rides in them today: `sessions` a factory taking the live project-path mapping and
returning the provider's conversation catalogue, `usage` a reader shaped like
`adapters.agents.<provider>.usage`'s (a `read(UsageQuery)` / `limits()` object), `hooks`
the provider name the hook-install surface in `adapters.agents.registry` accepts (each
vertical's `hooks.py` holds the configuration value), `activity` a declared placeholder —
`None` for all four providers until a vertical wires one — and `remote_control` the
host-level Remote Control object, wired only by Codex because only Codex has a toggle
whose subject is the machine rather than a pane.
"""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.models import ProfileId


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """One provider's declared capability set, keyed by its profile.

    `profile_id` is the one required field — a descriptor with no identity attaches its
    capabilities to nothing. Each capability defaults to `None` so a composition that wires
    only what a provider publishes constructs the honest record without ceremony.
    """

    profile_id: ProfileId
    """The curated-agent profile this descriptor speaks for."""

    sessions: object | None = None
    """A factory over the live project-path mapping returning the provider's conversation
    catalogue, or None when it exposes none."""

    usage: object | None = None
    """The provider's usage reader — read off its own files, per DEC-061 — or None when
    the provider publishes no usage at all. Absence is rendered, never estimated."""

    hooks: object | None = None
    """The provider name the hook-install surface accepts for this provider's hooks — the
    configuration value lives in the vertical's `hooks.py` — or None for a provider that
    takes no hooks."""

    activity: object | None = None
    """The provider's activity source, or None when it reports no activity events."""

    remote_control: object | None = None
    """The provider's host-level Remote Control, shaped like `ports.host_remote_control`'s
    protocol, or None when the provider has no host-level toggle.

    The sixth field, added deliberately (DEC-070) rather than grown into: only Codex
    publishes a Remote Control whose subject is *this machine*. Claude's toggle is a pane
    action and lives on the terminal port instead, so claude declares None here and is not
    thereby less capable — the two are different subjects, not two depths of one
    capability."""
