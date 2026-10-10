"""What OpenCode is now declared to report, and the two kinds it still cannot.

Until 2026-09-06 this provider was `UNOBSERVED`: the owner was told nothing about an OpenCode
session, ever. What changed is a measured plugin surface
(`docs/acceptance-2026-09-06-opencode-activity.md`), and what did not change is that a provider
publishing no equivalent of Claude's `StopFailure` cannot report a limit — the same line
Codex's licence already draws, drawn again rather than inherited.

The kinds are asserted *against the declared events* rather than restated, so an event added to
`INSTALLED_EVENTS` without a kind, or a kind claimed without an event to report it, fails here.
"""

from __future__ import annotations

from remote_agents.adapters.agents.opencode import descriptor
from remote_agents.adapters.agents.opencode.hooks import INSTALLED_EVENTS, RETIRED_EVENTS
from remote_agents.adapters.agents.registry import (
    activity_source_of,
    profile_reported_kinds,
    reported_kinds_of,
)
from remote_agents.ports.agent_activity import ActivityKind, ActivitySource

#: The profiles whose licence says they report alone, derived from the verticals (BL-050).
HOOK_SOURCED_PROFILES = frozenset(
    name
    for name in profile_reported_kinds()
    if activity_source_of(name) is ActivitySource.HOOK_EXCLUSIVE
)

#: Which activity kind each measured event becomes. The plugin's whole vocabulary.
_KIND_BY_EVENT = {
    "session.idle": ActivityKind.COMPLETED,
    "permission.asked": ActivityKind.NEEDS_ANSWER,
}


def test_opencode_is_watched_through_its_own_plugin_and_nothing_else() -> None:
    """Hook-exclusive, not hybrid: there is no pane marker to infer anything from."""
    assert activity_source_of("opencode") is ActivitySource.HOOK_EXCLUSIVE
    assert "opencode" in HOOK_SOURCED_PROFILES


def test_the_reported_kinds_are_exactly_what_the_declared_events_can_carry() -> None:
    """Derived from `INSTALLED_EVENTS`, so the two lists cannot drift apart silently."""
    assert reported_kinds_of("opencode") == {_KIND_BY_EVENT[event] for event in INSTALLED_EVENTS}


def test_opencode_claims_neither_limit_kind() -> None:
    """No `StopFailure` equivalent exists, so a limit is not something to guess from a pane."""
    kinds = reported_kinds_of("opencode")
    assert ActivityKind.LIMIT_REACHED not in kinds
    assert ActivityKind.OUTPUT_LIMIT not in kinds


def test_the_descriptor_declares_the_hook_name_the_installer_accepts() -> None:
    """One package, one descriptor, one registry entry (DEC-070) — including this capability."""
    assert descriptor().hooks == "opencode"


def test_the_retired_event_set_exists_from_this_provider_s_first_commit() -> None:
    """DEC-051: an event leaves `INSTALLED_EVENTS` by moving here, never by disappearing."""
    assert RETIRED_EVENTS == ()
    assert set(INSTALLED_EVENTS).isdisjoint(RETIRED_EVENTS)
