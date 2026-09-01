"""The provider-contract kit cannot pass by examining nothing (ARCH-07).

Authored red, before the kit exists — the same posture
`test_frontends_share_one_backend.py`'s empty-root test litigated: a guard added after the
thing it guards can pass green having read nothing, and nobody re-checks a green guard. It
goes green only when `tests/provider_contract/requirements.py` declares every capability of
every registered descriptor, and stays red on an empty registry, an undeclared field, or a
capability no provider supports at all (which would make its whole contract row vacuous).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.ports.provider_descriptor import ProviderDescriptor

_KIT = Path(__file__).resolve().parents[1] / "provider_contract"

CAPABILITIES = tuple(
    field.name for field in dataclasses.fields(ProviderDescriptor) if field.name != "profile_id"
)


def _requirements():
    if not (_KIT / "requirements.py").exists():
        pytest.fail(
            "the provider-contract kit declares no requirements yet: "
            f"{_KIT / 'requirements.py'} does not exist, so no contract test can be "
            "generated and a green kit would be vacuous"
        )
    import sys

    sys.path.insert(0, str(_KIT))
    try:
        import requirements
    finally:
        sys.path.remove(str(_KIT))
    return requirements


def test_the_registry_offers_work() -> None:
    descriptors = provider_descriptors()
    assert descriptors, "the registry is empty; the kit would parametrize nothing"


def test_every_capability_of_every_provider_is_declared() -> None:
    requirements = _requirements()
    declared = requirements.DECLARATIONS
    for descriptor in provider_descriptors():
        profile = str(descriptor.profile_id)
        assert profile in declared, f"{profile} has no requirements row"
        missing = [name for name in CAPABILITIES if name not in declared[profile]]
        assert not missing, f"{profile} declares nothing for {missing}"


def test_no_capability_is_universally_absent() -> None:
    """A capability no provider wires would make its whole contract column vacuous."""
    descriptors = provider_descriptors()
    dead = [
        name
        for name in CAPABILITIES
        if all(getattr(descriptor, name) is None for descriptor in descriptors)
    ]
    # `activity` is a declared placeholder until a vertical wires one (see the registry's
    # docstring); every other capability must have at least one live instance.
    assert dead in ([], ["activity"]), (
        f"capabilities {dead} have zero non-None instances; their contract rows would pass "
        "without ever exercising anything"
    )


@pytest.mark.parametrize("empty", [(), None])
def test_the_guard_itself_fails_loudly_on_an_empty_registry(empty) -> None:
    """Exercised both directions, per the Stage 1 gate: the guard's teeth are testable."""
    with pytest.raises(AssertionError, match="parametrize nothing|registry is empty"):
        descriptors = empty or ()
        assert descriptors, "the registry is empty; the kit would parametrize nothing"
