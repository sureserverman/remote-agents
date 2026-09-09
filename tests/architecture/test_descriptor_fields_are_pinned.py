"""`ProviderDescriptor`'s field set is pinned, so a seventh field is a decision, not drift.

The same treatment `Backend` gets in `test_frontends_share_one_backend.py`: the capability
set is read off the dataclass rather than restated, and pinned by length as well as by
content. DEC-061's clause matters here too — every capability is `<something> | None`,
because absence is a declared `None` a caller can read, never a probe and never an invented
value.
"""

from __future__ import annotations

import dataclasses
import types
import typing

#: The pin moved 5 -> 6 on 2026-09-03 by the plan
#: `2026-09-03-codex-remote-control-toggle-plan.md`, which added `remote_control`: the
#: host-level Remote Control toggle Codex publishes and the other three do not. Recorded
#: here rather than assumed, because DEC-070's whole claim is that growing this set is a
#: reviewable act and not drift.
#:
#: It moved 6 -> 7 on 2026-09-08 by the plan
#: `2026-09-08-...-sub-02-agent-glyphs-light-plan.md`, and that one grew the *identity*
#: half rather than this one: `glyph` is the mark a surface draws for a provider it has no
#: room to name. The two halves are pinned separately below because they obey opposite
#: rules -- a capability must admit `None` (DEC-061), an identity field must not.
#: It moved 7 -> 8 on 2026-09-09 by the plan
#: `2026-09-08-...-sub-05-answerable-dialogs-light-plan.md`, and this one grew the capability
#: half: `trust_dialog` is how an agent draws the folder-trust question, which is exactly a
#: capability with a real `None` -- `opencode` raises no such dialog on any host measured, and
#: that absence is what stops a Trust button appearing over a pane with no question on it.
#: It is also the first field carrying a *typed* value rather than an adapter object, which is
#: legal here for the reason the port's own docstring gives: five strings pull nothing into the
#: ports layer.
_IDENTITY_FIELDS = ("profile_id", "glyph")
_CAPABILITY_FIELDS = ("sessions", "usage", "hooks", "activity", "remote_control", "trust_dialog")


def _descriptor_fields() -> tuple[str, ...]:
    """`ProviderDescriptor`'s declared fields, read off the dataclass rather than restated."""
    from remote_agents.ports.provider_descriptor import ProviderDescriptor

    return tuple(ProviderDescriptor.__dataclass_fields__)


def test_the_descriptor_field_set_is_read_from_the_dataclass() -> None:
    """Eight fields: two identity, six capabilities. A ninth is a reviewable act."""
    fields = _descriptor_fields()
    expected = len(_IDENTITY_FIELDS) + len(_CAPABILITY_FIELDS)
    assert len(fields) == expected, (
        f"`ProviderDescriptor` now declares {len(fields)} fields, not {expected}. That may "
        "be fine — but every field is something a frontend reads as declared rather than "
        "discovers, so confirm the new field belongs here, decide whether it is identity or "
        "a capability, and update this pin deliberately."
    )
    assert fields[: len(_IDENTITY_FIELDS)] == _IDENTITY_FIELDS, (
        "the identity fields must come first and keep their order: both are required, and a "
        "required field declared after a defaulted one is a dataclass the interpreter refuses"
    )
    assert set(fields[len(_IDENTITY_FIELDS) :]) == set(_CAPABILITY_FIELDS)


def test_no_identity_field_is_optional() -> None:
    """The mirror of the capability rule: identity has no honest absence to declare.

    A `glyph` defaulting to `None` would let a fifth vertical join the registry
    indistinguishable from a sibling and pass every contract in the kit — which is exactly
    the defect DEC-070 put the field here to remove.
    """
    from remote_agents.ports import provider_descriptor

    hints = typing.get_type_hints(provider_descriptor.ProviderDescriptor)
    fields = provider_descriptor.ProviderDescriptor.__dataclass_fields__
    for name in _IDENTITY_FIELDS:
        annotation = hints[name]
        assert type(None) not in typing.get_args(annotation), (
            f"`{name}` admits None, so a provider could declare no {name} at all; identity "
            "is required, and only capabilities carry a declared absence (DEC-061)."
        )
        assert fields[name].default is dataclasses.MISSING, (
            f"`{name}` carries a default, so a vertical can acquire it by omission rather "
            "than by declaring it."
        )


def test_every_capability_field_is_declared_optional() -> None:
    """Capability absence is a declared `None` (DEC-061), so every annotation unions None."""
    from remote_agents.ports import provider_descriptor

    hints = typing.get_type_hints(provider_descriptor.ProviderDescriptor)
    for name in _CAPABILITY_FIELDS:
        annotation = hints[name]
        origin = typing.get_origin(annotation)
        assert origin in (typing.Union, types.UnionType), (
            f"`{name}` is annotated `{annotation!r}`, which is not a union at all; a "
            "capability must be `<something> | None` so a host that wired nothing is legible."
        )
        assert type(None) in typing.get_args(annotation), (
            f"`{name}` is a union that does not admit None; capability absence must be a "
            "declared None, per DEC-061."
        )
