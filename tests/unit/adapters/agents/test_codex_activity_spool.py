"""What a Codex hook payload is allowed to put on disk, measured before it was parsed.

The field names below are not guesses: `docs/acceptance-2026-08-29-codex-activity-detail.md`
records them from real payloads captured against a disposable `CODEX_HOME`. That document's
"What this licenses the next task to parse" section is the contract these cases pin, and the
reason it exists is in `_DISCRIMINATING_FIELDS`' own comment -- `error_type` and `end_reason`
were assumed from a symbol table, were both wrong, and made `limit_reached` unreachable in
silence.

Every payload here is built from the measured vocabulary and then deliberately *over-filled*
with the fields the measurement showed are dangerous, so a case fails if the parser widens.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO

from remote_agents.adapters.agents.activity_spool import _observed_event
from remote_agents.ports.agent_activity import MAXIMUM_DETAIL_CHARACTERS


def _codex(payload: dict) -> object:
    return _observed_event(
        BytesIO(json.dumps(payload).encode("utf-8")), "session", datetime.now(UTC), "codex"
    )


#: A `Stop` payload with every field the measurement observed, values chosen so any leak is
#: recognisable in an assertion.
_MEASURED_STOP = {
    "session_id": "provider-session-not-ours",
    "turn_id": "turn-0",
    "transcript_path": None,
    "cwd": "/home/owner/secret-project",
    "hook_event_name": "Stop",
    "model": "gpt-5.6-sol",
    "permission_mode": "default",
    "stop_hook_active": False,
    "last_assistant_message": "Ran the suite and pushed the branch.",
}

#: A `PermissionRequest` payload, likewise. Note `transcript_path` is a real path on this event
#: and `tool_input` carries the literal command -- both measured, both forbidden.
_MEASURED_PERMISSION = {
    "session_id": "provider-session-not-ours",
    "turn_id": "turn-0",
    "transcript_path": "/home/owner/.codex/sessions/rollout-secret.jsonl",
    "cwd": "/home/owner/secret-project",
    "hook_event_name": "PermissionRequest",
    "model": "gpt-5.6-sol",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {
        "command": "rm -rf /home/owner/secret-project",
        "description": "Do you want to allow deleting /home/owner/secret-project?",
    },
}


def test_a_codex_stop_carries_the_agents_own_last_line() -> None:
    """The whole point of the sub-plan: a Codex `completed` stops arriving as a bare sentence.

    `last_assistant_message` is the field the measurement found, and it is the same name Claude's
    `Stop` carries -- which is why this is a widening of an existing path rather than a new one.
    """
    observed = _codex(_MEASURED_STOP)

    assert observed is not None
    assert observed.event == "Stop"
    assert observed.detail == "Ran the suite and pushed the branch."


def test_a_codex_stop_detail_is_bounded_exactly_as_claudes_is() -> None:
    """One line, bounded, or nothing -- the budget both ends of the spool agree on."""
    observed = _codex({**_MEASURED_STOP, "last_assistant_message": "x " * 4000})

    assert observed is not None
    assert observed.detail is not None
    assert len(observed.detail) <= MAXIMUM_DETAIL_CHARACTERS
    assert "\n" not in observed.detail


def test_a_codex_permission_request_stays_content_free() -> None:
    """It admits nothing, and "nothing" is a narrowing of what this task set out to do.

    The measurement found `tool_name` is the *only* field on this event that names the ask
    without carrying a command, a path or a prompt -- `tool_input.command` is the literal
    command, `tool_input.description` restates the path, and `transcript_path` is populated here
    unlike on `Stop`. So the plan expected to admit `tool_name`.

    Safe is not the same as needed. `ObservedAgentEvent.reason` exists for exactly one purpose:
    `application/activity._kind` reads it to choose an `ActivityKind`. For `PermissionRequest`
    that choice is unconditional -- `NEEDS_ANSWER, REPORTED`, whatever the reason says -- and
    `AgentActivity` has no `reason` field, so nothing downstream renders it. Admitting
    `tool_name` would write a provider string to a file on disk that no surface ever shows and
    no decision ever consults. That is retention without rendering, which is the thing DEC-013
    bounds and the thing DEC-063's content-free claim is about.

    So this event keeps the behaviour it already had, and the sub-plan's goal is met by the
    `Stop` half alone. Rendering the tool class would be a real improvement to ask 4 and is a
    renderer change, not a spool change; it is raised rather than smuggled in under this task.
    """
    observed = _codex(_MEASURED_PERMISSION)

    assert observed is not None
    assert observed.event == "PermissionRequest"
    assert observed.reason is None, (
        "nothing renders a reason for this event; storing one is retention"
    )
    assert observed.detail is None, "a permission request carries no agent words to render"


def test_no_codex_payload_field_naming_a_path_command_or_prompt_reaches_disk() -> None:
    """DEC-063's retention bound, asserted on the serialized record rather than on a field.

    Written against the *whole* document that reaches the spool file, because the failure this
    prevents is not "we read the wrong field" but "something we read carried more than we
    thought". A future field added upstream fails here without anyone having to predict its name.
    """
    forbidden = (
        "/home/owner/secret-project",
        "/home/owner/.codex/sessions/rollout-secret.jsonl",
        "rm -rf",
        "Do you want to allow deleting",
        "provider-session-not-ours",
    )
    for payload in (_MEASURED_STOP, _MEASURED_PERMISSION):
        observed = _codex(payload)
        assert observed is not None
        rendered = json.dumps(observed.document())
        for secret in forbidden:
            assert secret not in rendered, (
                f"{secret!r} reached the spool from {payload['hook_event_name']}"
            )


def test_the_codex_event_allow_list_is_unchanged() -> None:
    """Widening what a payload carries must not widen which events are admitted."""
    for event in ("SessionEnd", "PreToolUse", "PostToolUse", "Notification", "UserPromptSubmit"):
        assert _codex({**_MEASURED_STOP, "hook_event_name": event}) is None


def test_a_codex_stop_without_the_field_is_still_spooled() -> None:
    """A payload shape this build has not seen must degrade to no detail, never to no record.

    The measurement is of one build on one host. A `Stop` that omits the field -- an older
    Codex, a newer one, a turn that produced no assistant message -- is still news that the
    agent stopped, and dropping it would trade a missing sentence for a missing notification.
    """
    without = {k: v for k, v in _MEASURED_STOP.items() if k != "last_assistant_message"}

    observed = _codex(without)

    assert observed is not None and observed.event == "Stop"
    assert observed.detail is None


def test_claude_parsing_is_untouched_by_the_codex_widening() -> None:
    """The two providers share this function; only the Codex branch changed."""
    observed = _observed_event(
        BytesIO(
            json.dumps(
                {
                    "hook_event_name": "Stop",
                    "last_assistant_message": "Claude's line.",
                    "error": "rate_limit",
                }
            ).encode("utf-8")
        ),
        "session",
        datetime.now(UTC),
        "claude",
    )

    assert observed is not None
    assert observed.detail == "Claude's line."
    assert observed.reason == "rate_limit"
