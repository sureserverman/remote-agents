"""Every kit skip is a declaration's doing — the reconciliation is derived, not eyeballed.

One skip per UNSUPPORTED declaration plus one per CONDITIONAL declaration whose condition
is unmet (the capability wired None). The Stage 1 gate performed the live reconciliation
once — summed the run's SKIPPED report and compared it to this derivation (6 == 6) — and
what stands guard afterwards is this pin: a declaration change moves the derived number and
fails here, prompting the gate's summed-grep comparison to be re-run rather than trusted.
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
