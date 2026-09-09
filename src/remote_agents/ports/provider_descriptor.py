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

from dataclasses import MISSING, dataclass, fields

from remote_agents.domain.models import ProfileId


@dataclass(frozen=True, slots=True)
class TrustDialog:
    """How one agent draws the folder-trust question, so this project can read and answer it.

    Values only — the arithmetic that turns them into keypresses is
    `adapters/tmux/trust.py`'s, and the sentence around the answer is the bot's (DEC-043).
    Every field here was read off a real pane and is recorded in
    `docs/acceptance-2026-09-09-trust-dialogs.md`; a field carried from an older version says
    so there rather than pretending to be a measurement.

    **`identifies_by` exists because the question does not identify the agent.** `codex` and
    `cursor-agent` draw *"Do you trust the contents of this directory?"* **verbatim**, and it
    is currently codex's readiness blocker. Answering one agent's dialog with another's row
    arithmetic is not a near miss: codex rests its cursor on the affirmative and claude on the
    negative, so the confirming keypress lands on exactly the wrong option and the owner who
    pressed *Trust* watches the agent quit. So a dialog is recognised by a string only its own
    agent draws, cross-checked in `tests/provider_contract` against every other agent's real
    capture rather than against its siblings' declarations.

    Short strings, deliberately. A pane capture wraps at the pane's width, so a long sentence
    can be split across two rows at a width nobody measured — and an identifier that wraps is
    an identifier that vanishes exactly when the dialog is on screen.
    """

    question: str
    """The sentence the dialog asks. Not an identifier — two agents share one word for word."""

    affirmative: str
    """The row that answers *yes*, matched as a substring: the numbering some versions carry
    (`1. Yes, continue`) and others do not is not part of the contract."""

    negative: str
    """The row that answers *no*, matched the same way."""

    cursor: str
    """The one character this agent draws on the row its selection rests on.

    One character, and not anchored to the start of a line: `cursor-agent` draws its dialog
    inside a box, so its `▶` sits behind a `│` and two spaces. A parser that looked for a row
    *starting* with the glyph would find nothing there.
    """

    identifies_by: str
    """The substring only this agent draws, which is what says whose dialog is on screen."""


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """One provider's declared capability set, keyed by its profile.

    `profile_id` and `glyph` are the two required fields — the identity half of the record.
    A descriptor with no identity attaches its capabilities to nothing, and one with no mark
    attaches them to a provider the surfaces cannot tell apart. Each *capability* defaults to
    `None` so a composition that wires only what a provider publishes constructs the honest
    record without ceremony; that default is also what separates the two halves, since an
    identity field has no honest absence to declare.
    """

    profile_id: ProfileId
    """The curated-agent profile this descriptor speaks for."""

    glyph: str
    """This provider's mark, for a surface with room for one character and not a name.

    Required, with no default, because DEC-009's "no third answer" argument applies: a
    provider that declared none would be one the owner cannot distinguish from another in the
    same project, which is the defect the field exists to remove — and a default would let a
    fifth vertical acquire that defect silently. It is the *token* only; the sentence around
    it belongs to whichever surface renders it (DEC-043), and no two providers may declare
    the same one.

    Not a capability, though it sits in the same record: there is no honest `None` here to
    read. That distinction is load-bearing for the contract kit, which drives capabilities
    from a requirements table and identity unconditionally.
    """

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

    trust_dialog: TrustDialog | None = None
    """How this agent asks about folder trust, or None when it never asks.

    A capability with a real absence, and the `None` is what makes the surfaces honest in the
    one direction that costs something: `opencode` raises no such dialog on any host measured,
    so a Trust button on an `opencode` session would send arrow keys and an Enter into a live
    prompt with no question on it. DEC-009 — the absence is declared by the vertical, never
    inferred from a missing entry somewhere else.

    Typed, unlike its neighbours, because it carries no adapter: a `TrustDialog` is five
    strings, so naming it here pulls nothing into the ports layer that was not already here.
    """

    remote_control: object | None = None
    """The provider's host-level Remote Control, shaped like `ports.host_remote_control`'s
    protocol, or None when the provider has no host-level toggle.

    The sixth field, added deliberately (DEC-070) rather than grown into: only Codex
    publishes a Remote Control whose subject is *this machine*. Claude's toggle is a pane
    action and lives on the terminal port instead, so claude declares None here and is not
    thereby less capable — the two are different subjects, not two depths of one
    capability."""


def capability_fields() -> tuple[str, ...]:
    """Which of this record's fields are capabilities, as opposed to identity.

    The predicate is structural rather than a list of names to keep updated: a capability
    is a field whose default is `None`, because DEC-061 is what makes it one — absence is a
    *declared* answer a frontend reads with `is None`, so a field with no such absence to
    declare is identity (`profile_id`, `glyph`), required and always present.

    It lives here, beside the dataclass, because it is a statement about this record and not
    about any one caller's use of it. Two test modules gate the provider-contract kit on
    exactly this set — every capability of every provider must carry a requirements
    declaration — and they held one copy of the predicate each. The copies agreed on the day
    they were written; nothing made them keep agreeing, and each called itself "the"
    capability set.
    """
    return tuple(
        field.name
        for field in fields(ProviderDescriptor)
        if field.default is None and field.default_factory is MISSING
    )
