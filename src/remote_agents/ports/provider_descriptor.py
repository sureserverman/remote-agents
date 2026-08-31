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
actually rides in them today: `sessions` a provider conversation source, `usage` a reader
shaped like `adapters.agents.usage`'s (a `read(UsageQuery)` / `limits()` object), `hooks` an
installer shaped like `adapters.agents.hook_install`'s, `activity` an activity spool source.
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
    """The provider's conversation/session source, or None when it exposes none."""

    usage: object | None = None
    """The provider's usage reader — read off its own files, per DEC-061 — or None when
    the provider publishes no usage at all. Absence is rendered, never estimated."""

    hooks: object | None = None
    """The provider's hook installer, or None for a provider that takes no hooks."""

    activity: object | None = None
    """The provider's activity source, or None when it reports no activity events."""
