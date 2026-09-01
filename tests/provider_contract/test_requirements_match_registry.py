"""The requirements table and the registry must agree about every capability's None-ness."""

from __future__ import annotations

import dataclasses

import pytest
from requirements import DECLARATIONS, Requirement

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.ports.provider_descriptor import ProviderDescriptor

CAPABILITIES = tuple(
    field.name for field in dataclasses.fields(ProviderDescriptor) if field.name != "profile_id"
)


def _rows():
    for descriptor in provider_descriptors():
        for capability in CAPABILITIES:
            yield str(descriptor.profile_id), capability, getattr(descriptor, capability)


@pytest.mark.parametrize(("profile", "capability", "wired"), list(_rows()))
def test_every_capability_has_exactly_one_declared_state(profile, capability, wired) -> None:
    declared = DECLARATIONS[profile][capability]
    assert isinstance(declared, tuple) and isinstance(declared[0], Requirement), declared
    state, reason = declared
    assert reason, f"{profile}.{capability} declares no reason"
    if state is Requirement.SUPPORTED:
        assert wired is not None, (
            f"{profile}.{capability} is declared supported but the registry wired None — "
            "the declaration invents a capability (DEC-061)"
        )
    if state is Requirement.UNSUPPORTED:
        assert wired is None, (
            f"{profile}.{capability} is declared unsupported but the registry wired "
            f"{type(wired).__name__} — the declaration hides real coverage"
        )


def test_no_declaration_names_an_unknown_provider_or_capability() -> None:
    profiles = {str(descriptor.profile_id) for descriptor in provider_descriptors()}
    assert set(DECLARATIONS) == profiles
    for profile, row in DECLARATIONS.items():
        assert set(row) == set(CAPABILITIES), (profile, sorted(row))
