"""Three-state capability requirements per provider: the kit's single source of skips.

The SQLAlchemy dialect-suite shape: a contract test never writes `if x is None: skip` at
the test site — it asks this table, and the table answers SUPPORTED (drive it),
UNSUPPORTED (a named skip, visible in `pytest -rs`, carrying the declaration that caused
it), or CONDITIONAL (drive it when the named precondition holds). Every declaration must
match the registry's None-ness exactly (DEC-061: absence is declared, never invented —
in both directions: declaring `unsupported` over a wired capability hides coverage, and
`supported` over a None invents it). `test_requirements_match_registry.py` enforces the
agreement; the architecture tree's vacuity guard enforces completeness.
"""

from __future__ import annotations

from enum import Enum


class Requirement(Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONDITIONAL = "conditional"


SUPPORTED = Requirement.SUPPORTED
UNSUPPORTED = Requirement.UNSUPPORTED
CONDITIONAL = Requirement.CONDITIONAL

#: The capabilities CONDITIONAL may legally describe — a closed, pinned set, because an
#: unrestricted CONDITIONAL is an escape hatch from the supported/unsupported agreement
#: (declare a wired capability conditional and it silently stops being driven-or-refused).
#: Growing this set is a reviewed act with a reason, exactly like growing an allowlist.
CONDITIONAL_CAPABILITIES = frozenset({"activity"})

#: profile id -> capability -> declared state, with the reason a skip will carry.
#: `activity` is CONDITIONAL everywhere: the registry declares it a placeholder until a
#: vertical wires one, so the kit neither drives nor mourns it — the condition is "a
#: vertical wired it", currently false for all four.
DECLARATIONS: dict[str, dict[str, tuple[Requirement, str]]] = {
    "claude": {
        "sessions": (SUPPORTED, "transcript catalogue over the workspace mapping"),
        "usage": (SUPPORTED, "transcript accounting plus the borrowed status-line cache"),
        "hooks": (SUPPORTED, "settings.json hook groups (claude, flagless)"),
        "activity": (CONDITIONAL, "placeholder until the vertical wires an activity source"),
    },
    "codex": {
        "sessions": (SUPPORTED, "rollout catalogue via the app-server client"),
        "usage": (SUPPORTED, "rollout token_count records, session and account-wide"),
        "hooks": (SUPPORTED, "hooks.json hook groups (codex, flagged)"),
        "activity": (CONDITIONAL, "placeholder until the vertical wires an activity source"),
    },
    "opencode": {
        "sessions": (SUPPORTED, "opencode.db catalogue via the CLI runner"),
        "usage": (SUPPORTED, "opencode.db message-token accounting"),
        "hooks": (UNSUPPORTED, "opencode takes no hooks; the registry declares None"),
        "activity": (CONDITIONAL, "placeholder until the vertical wires an activity source"),
    },
    "cursor-agent": {
        "sessions": (SUPPORTED, "constant catalogue; workspace-blind by design"),
        "usage": (SUPPORTED, "constant-empty answer: publishes nothing, honestly (DEC-061)"),
        "hooks": (UNSUPPORTED, "cursor takes no hooks; the registry declares None"),
        "activity": (CONDITIONAL, "placeholder until the vertical wires an activity source"),
    },
}
