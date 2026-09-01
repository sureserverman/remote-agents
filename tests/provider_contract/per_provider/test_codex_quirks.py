"""Codex's discriminating behavior, driven from the measured-vocabulary fixtures.

Moved whole from `tests/unit/adapters/agents/test_codex_activity_spool.py` when the
provider-contract kit landed: the payloads became `fixtures/codex/*.json` (each carrying its
capture provenance) and the assertions kept their reasons verbatim. The field names are not
guesses: `docs/acceptance-2026-08-29-codex-activity-detail.md` records them from real
payloads captured against a disposable `CODEX_HOME`; `error_type` and `end_reason` were once
assumed from a symbol table, were both wrong, and made `limit_reached` unreachable in
silence (DEC-067). Every fixture is deliberately *over-filled* with the fields the
measurement showed are dangerous, so a case fails if the parser widens.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from remote_agents.adapters.agents.activity_spool import _observed_event
from remote_agents.ports.agent_activity import MAXIMUM_DETAIL_CHARACTERS

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _codex(payload: dict) -> object:
    return _observed_event(
        BytesIO(json.dumps(payload).encode("utf-8")), "session", datetime.now(UTC), "codex"
    )


def test_a_codex_stop_carries_the_agents_own_last_line() -> None:
    """The whole point of the sub-plan: a Codex `completed` stops arriving as a bare sentence.

    `last_assistant_message` is the field the measurement found, and it is the same name
    Claude's `Stop` carries -- which is why this is a widening of an existing path rather
    than a new one.
    """
    observed = _codex(_fixture("stop.json"))

    assert observed is not None
    assert observed.event == "Stop"
    assert observed.detail == "Ran the suite and pushed the branch."


def test_a_codex_stop_detail_is_bounded_exactly_as_claudes_is() -> None:
    """One line, bounded, or nothing -- the budget both ends of the spool agree on."""
    observed = _codex({**_fixture("stop.json"), "last_assistant_message": "x " * 4000})

    assert observed is not None
    assert observed.detail is not None
    assert len(observed.detail) <= MAXIMUM_DETAIL_CHARACTERS
    assert "\n" not in observed.detail


def test_a_codex_permission_request_stays_content_free() -> None:
    """It admits nothing, and "nothing" is a narrowing of what this task set out to do.

    The measurement found `tool_name` is the *only* field on this event that names the ask
    without carrying a command, a path or a prompt. What survives is narrower still:
    `detail` means *the agent's own words*, and a bare provider token is a different kind of
    string in a field every consumer reads as a sentence the agent wrote. DEC-067 records
    both the conclusion and the corrected reasoning (the original file's docstring carries
    the full argument; kept there in history rather than restated wrong).
    """
    observed = _codex(_fixture("permission_request.json"))

    assert observed is not None
    assert observed.event == "PermissionRequest"
    assert observed.reason is None, (
        "nothing renders a reason for this event; storing one is retention"
    )
    assert observed.detail is None, "a permission request carries no agent words to render"


def test_no_codex_payload_field_naming_a_path_command_or_prompt_reaches_disk() -> None:
    """DEC-063's retention bound, asserted on the serialized record rather than on a field.

    Written against the *whole* document that reaches the spool file: a future field added
    upstream fails here without anyone having to predict its name.
    """
    forbidden = (
        "/home/owner/secret-project",
        "/home/owner/.codex/sessions/rollout-secret.jsonl",
        "rm -rf",
        "Do you want to allow deleting",
        "provider-session-not-ours",
    )
    for name in ("stop.json", "permission_request.json"):
        payload = _fixture(name)
        observed = _codex(payload)
        assert observed is not None
        rendered = json.dumps(observed.document())
        for secret in forbidden:
            assert secret not in rendered, (
                f"{secret!r} reached the spool from {payload['hook_event_name']}"
            )


def test_the_codex_event_allow_list_is_unchanged() -> None:
    """Widening what a payload carries must not widen which events are admitted."""
    stop = _fixture("stop.json")
    for event in ("SessionEnd", "PreToolUse", "PostToolUse", "Notification", "UserPromptSubmit"):
        assert _codex({**stop, "hook_event_name": event}) is None


def test_a_codex_stop_without_the_field_is_still_spooled() -> None:
    """A payload shape this build has not seen must degrade to no detail, never to no record."""
    without = {k: v for k, v in _fixture("stop.json").items() if k != "last_assistant_message"}

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
