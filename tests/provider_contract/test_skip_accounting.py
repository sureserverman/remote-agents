"""Every kit skip is a declaration's doing — the reconciliation is derived, not eyeballed.

The Stage 1 gate compares `pytest tests/provider_contract -rs -q | grep -c SKIPPED`
against the number this test derives: one skip per UNSUPPORTED declaration plus one per
CONDITIONAL declaration whose condition is unmet (the capability wired None). Deriving it
here means the gate's grep has a computed sibling that fails when a declaration and the
kit's skip behavior drift apart, instead of a human comparing two numbers once.
"""

from __future__ import annotations

from requirements import DECLARATIONS, Requirement

from remote_agents.adapters.agents.registry import provider_descriptors


def expected_skips() -> int:
    skips = 0
    for descriptor in provider_descriptors():
        for capability, (state, _reason) in DECLARATIONS[str(descriptor.profile_id)].items():
            if state is Requirement.UNSUPPORTED:
                skips += 1
            elif state is Requirement.CONDITIONAL and getattr(descriptor, capability) is None:
                skips += 1
    return skips


def test_the_skip_count_is_fully_accounted_for() -> None:
    assert expected_skips() == 6, (
        "the kit's skip budget changed; re-derive the gate's grep expectation from this "
        "number rather than editing either side alone"
    )
