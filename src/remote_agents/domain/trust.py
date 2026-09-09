"""Typed state for the folder-trust question a managed launch can block on."""

from collections.abc import Mapping
from enum import StrEnum

from remote_agents.domain.models import ProfileId


class TrustState(StrEnum):
    AWAITING = "awaiting"
    UNKNOWN = "unknown"


#: **`TRUST_ANSWERABLE` was here, and it was a list.** A frozenset naming claude and
#: claude-remote, on the argument that the *yes* may only be sent to an agent whose dialog
#: this project can read — which was true, and which the list then stated in a place that
#: could not tell whether it was still true. codex and cursor-agent both ask, both were
#: readable in principle, and neither was in the set; the owner pressed Trust on a codex
#: session and got one button where the ask said two.
#:
#: It is now derived: a profile is answerable exactly when its vertical declares a
#: `trust_dialog` (`adapters/agents/registry.profile_trust_dialogs`, DEC-070). The domain
#: keeps the *shape* of the question below rather than the answer, so the policy and the
#: runtime still cannot disagree — they are handed the same mapping by the composition root,
#: which is the one place allowed to know both (DEC-001).
def answerable(profile_id: ProfileId, dialogs: Mapping[str, object]) -> bool:
    """Whether this profile's agent draws a dialog this project knows how to read.

    A function over a supplied mapping rather than a module constant, because the answer is a
    *provider* fact and the domain may not import a provider package.

    `Mapping[str, object]` and not `Mapping[str, TrustDialog | None]` for that same reason and
    no other: `TrustDialog` lives in `ports`, the domain is the innermost layer and imports
    neither ports nor adapters (DEC-001), and a type-only import would be a boundary crossed
    for the convenience of a checker this repo does not run. The looseness is real — a mapping
    of anything at all type-checks here — and it is bounded by there being exactly one producer
    of the mapping (`adapters.agents.registry.profile_trust_dialogs`) and a contract test
    asserting what it contains. Both callers — the
    application's availability policy and the terminal that presses the keys — receive the
    same mapping, so the one property the old constant existed to guarantee is kept: a surface
    cannot offer a button that the runtime will then refuse.
    """
    return dialogs.get(str(profile_id)) is not None
