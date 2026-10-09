"""Every kit skip is a declaration's doing — the reconciliation is derived, not eyeballed.

One skip per UNSUPPORTED declaration plus one per CONDITIONAL declaration whose condition
is unmet (the capability wired None). The Stage 1 gate performed the live reconciliation
once — summed the run's SKIPPED report and compared it to this derivation (6 == 6) — and
what stands guard afterwards is this pin: a declaration change moves the derived number and
fails here, prompting the gate's summed-grep comparison to be re-run rather than trusted.

**9 -> 8 on 2026-09-06**, and the pin doing its job is the record of why: opencode's `hooks`
stopped being an UNSUPPORTED declaration when the provider gained a plugin installer, so one
skip became one driven contract test. The number was re-derived and the live comparison re-run
at that stage's gate, which is exactly the prompt this assertion exists to produce.

**8 -> 9 on 2026-09-09**, the same mechanism in the other direction: `trust_dialog` joined the
descriptor and `opencode` declares it UNSUPPORTED, because it raises no folder-trust dialog on
any host measured. It caught something on the way, which is the better half of the record: the
first version of `test_trust_dialog_contracts.py` wrote its own `if state is UNSUPPORTED`
branch instead of calling `drive_or_skip`, so the derivation said 9 while the run reported 8 --
a declared skip that never became a skip. The test was moved onto the kit's own idiom and the
live comparison re-run (9 == 9).
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
    # 9 -> 12 on 2026-09-28: `limit_screen` joined the capability set declared for codex,
    # opencode and cursor-agent as unsupported (Sub-plan 1 Task 2.1 of the limit-lifecycle plan).
    # 12 -> 10 the same day: Task 2.2 declared codex's and cursor-agent's.
    # 10 -> 9 on 2026-10-09: cursor-agent's `hooks` became SUPPORTED (BL-106), so one declared
    # skip became a driven contract test. The live run, re-summed that day, reported 10 SKIPPED
    # lines from these 9 declarations: opencode's one `trust_dialog` declaration skips two tests.
    assert expected_skips() == 9, (
        "the kit's skip budget changed; re-derive the gate's grep expectation from this "
        "number rather than editing either side alone"
    )
