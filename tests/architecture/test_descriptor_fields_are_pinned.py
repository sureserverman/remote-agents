"""`ProviderDescriptor`'s field set is pinned, so a sixth field is a decision, not drift.

The same treatment `Backend` gets in `test_frontends_share_one_backend.py`: the capability
set is read off the dataclass rather than restated, and pinned by length as well as by
content. DEC-061's clause matters here too — every capability is `<something> | None`,
because absence is a declared `None` a caller can read, never a probe and never an invented
value.
"""

from __future__ import annotations

import types
import typing

_CAPABILITY_FIELDS = ("sessions", "usage", "hooks", "activity")


def _descriptor_fields() -> tuple[str, ...]:
    """`ProviderDescriptor`'s declared fields, read off the dataclass rather than restated."""
    from remote_agents.ports.provider_descriptor import ProviderDescriptor

    return tuple(ProviderDescriptor.__dataclass_fields__)


def test_the_descriptor_field_set_is_read_from_the_dataclass() -> None:
    """Five fields: one identity, four capabilities. A sixth is a reviewable act."""
    fields = _descriptor_fields()
    assert len(fields) == 5, (
        f"`ProviderDescriptor` now declares {len(fields)} fields, not 5. That may be fine — "
        "but every field is a capability a frontend reads as declared rather than discovers, "
        "so confirm the new field belongs here and update this pin deliberately."
    )
    assert fields[0] == "profile_id"
    assert set(fields[1:]) == set(_CAPABILITY_FIELDS)


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
