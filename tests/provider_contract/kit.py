"""Registry parametrization for the provider-contract kit (ARCH-07).

`pytest_generate_tests` walks the live registry at collection time — DEC-070's accepted
cost 2, measured at the Stage 3 gate — so a fifth provider's descriptor is driven the day
its registry entry lands, with no test edit. Each contract test receives one descriptor;
whether it drives, skips, or conditions is the requirements table's answer alone
(`requirements.py`), and an `unsupported` skip carries the declaration's reason — a named
mechanism deliberately distinct from `requires_session` (DEC-059: what is not run is a
named set, never an absence).
"""

from __future__ import annotations

import pytest
from requirements import DECLARATIONS, Requirement

from remote_agents.adapters.agents.registry import provider_descriptors


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "descriptor" in metafunc.fixturenames:
        descriptors = provider_descriptors()
        metafunc.parametrize(
            "descriptor",
            descriptors,
            ids=[str(descriptor.profile_id) for descriptor in descriptors],
        )


def requirement_for(descriptor, capability: str):
    state, reason = DECLARATIONS[str(descriptor.profile_id)][capability]
    return state, reason


def drive_or_skip(descriptor, capability: str):
    """Apply the declared state; return the wired capability object when driving."""
    state, reason = requirement_for(descriptor, capability)
    if state is Requirement.UNSUPPORTED:
        pytest.skip(f"unsupported by declaration: {reason} [requirements.py]")
    if state is Requirement.CONDITIONAL:
        wired = getattr(descriptor, capability)
        if wired is None:
            pytest.skip(f"conditional, condition unmet: {reason} [requirements.py]")
        return wired
    wired = getattr(descriptor, capability)
    assert wired is not None, (
        f"{descriptor.profile_id}.{capability} declared supported but wired None"
    )
    return wired
