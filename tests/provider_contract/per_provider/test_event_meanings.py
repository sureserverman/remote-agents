"""What each provider's events mean, as its licence declares it (BL-131).

The drain used to decide this from the event's name, in shared code. These are the meanings it
gave each event then, now read from the licence: a change here is a change in what the owner is
told, so each one is pinned by name.
"""

from __future__ import annotations

from remote_agents.adapters.agents.claude.hooks import AGENTS_VIEW
from remote_agents.adapters.agents.claude.hooks import HOOK_LICENCE as CLAUDE
from remote_agents.adapters.agents.codex.hooks import HOOK_LICENCE as CODEX
from remote_agents.adapters.agents.cursor.hooks import HOOK_LICENCE as CURSOR
from remote_agents.adapters.agents.opencode.hooks import HOOK_LICENCE as OPENCODE
from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.provider_descriptor import TurnEdge


def test_claude_events_mean_what_the_drain_said_they_meant() -> None:
    events = CLAUDE.events

    assert events["Stop"].kind is ActivityKind.COMPLETED
    assert events["Stop"].turn is TurnEdge.ENDS
    assert events["StopFailure"].kind is None
    assert dict(events["StopFailure"].kinds_by_reason) == {
        "rate_limit": ActivityKind.LIMIT_REACHED,
        "max_output_tokens": ActivityKind.OUTPUT_LIMIT,
    }
    assert events["StopFailure"].turn is TurnEdge.ENDS
    assert dict(events["Notification"].kinds_by_reason) == {
        "agent_needs_input": ActivityKind.NEEDS_ANSWER
    }
    assert dict(events["Notification"].detail_lead_by_reason) == {"agent_needs_input": AGENTS_VIEW}
    assert events["Notification"].turn is None
    assert events["PermissionRequest"].kind is ActivityKind.NEEDS_ANSWER
    assert events["UserPromptSubmit"].turn is TurnEdge.STARTS
    assert events["UserPromptSubmit"].kind is None
    assert CLAUDE.starts_turn_marker


def test_codex_events_mean_what_the_drain_said_they_meant() -> None:
    events = CODEX.events

    assert events["Stop"].kind is ActivityKind.COMPLETED
    assert events["Stop"].turn is TurnEdge.ENDS
    assert events["PermissionRequest"].kind is ActivityKind.NEEDS_ANSWER
    assert events["UserPromptSubmit"].turn is TurnEdge.STARTS
    assert CODEX.starts_turn_marker


def test_opencode_and_cursor_events_mean_what_the_drain_said_they_meant() -> None:
    assert OPENCODE.events["session.idle"].kind is ActivityKind.COMPLETED
    assert OPENCODE.events["permission.asked"].kind is ActivityKind.NEEDS_ANSWER
    assert CURSOR.events["stop"].kind is ActivityKind.COMPLETED
    # Neither marks turns: no event of theirs ends one (DEC-104).
    assert not OPENCODE.starts_turn_marker
    assert not CURSOR.starts_turn_marker
    for licence in (OPENCODE, CURSOR):
        assert all(event.turn is None for event in licence.events.values())


def test_only_codex_draws_an_approval_title() -> None:
    drawn = {
        str(descriptor.profile_id)
        for descriptor in provider_descriptors()
        if descriptor.action_required_title is not None
    }

    assert drawn == {"codex"}
    pattern = next(
        d.action_required_title for d in provider_descriptors() if str(d.profile_id) == "codex"
    )
    assert pattern.match("[ ! ] Action Required | remote-agents")
    assert pattern.match("[ . ] Action Required | remote-agents")
    assert not pattern.match("remote-agents [ ! ] Action Required | x")
