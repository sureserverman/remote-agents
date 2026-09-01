"""Claude's discriminating behavior, driven from the requirement-built fixtures.

Unlike Codex's, these fixtures were *not* rebuilt from a capture document: the field
vocabulary is the one the parser itself declares -- `_DISCRIMINATING_FIELDS`,
`_DETAIL_FIELDS` and the claude branch of `_observed_event` in
`src/remote_agents/adapters/agents/activity_spool.py`, whose own comment records the
measurement against the installed claude bundle 2.1.227 that corrected `error_type` and
`end_reason` -- together with the claude cases the shared unit suite already exercises.
Values are synthetic per GDEC-SEC-001 and every fixture is deliberately *over-filled*
with the dangerous fields (`transcript_path`, `cwd`, `prompt_id`, the provider's own
`session_id`), so a case fails if the parser widens.

The shared `tests/unit/adapters/agents/test_activity_spool.py` stays by design: it holds
the spool's generic machinery claims (private write, symlink refusal, session-id safety,
malformed and oversized payloads, name collisions) with claude cases mixed in, and
retiring it would leave those shared-machinery claims ownerless. This module owns the
*per-provider* half only: which field each claude event discriminates on, what detail
survives, and what never reaches disk.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from remote_agents.adapters.agents.activity_spool import _observed_event
from remote_agents.ports.agent_activity import MAXIMUM_DETAIL_CHARACTERS

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _claude(payload: dict) -> object:
    return _observed_event(
        BytesIO(json.dumps(payload).encode("utf-8")), "session", datetime.now(UTC), "claude"
    )


def test_a_claude_stop_carries_the_agents_own_last_line() -> None:
    """`last_assistant_message` is the allowance DEC-013 clause (2) has always granted claude."""
    observed = _claude(_fixture("stop.json"))

    assert observed is not None
    assert observed.event == "Stop"
    assert observed.reason is None
    assert observed.detail == "Ran the suite and pushed the branch."


@pytest.mark.parametrize(
    ("name", "event", "reason", "detail"),
    [
        ("stop_failure.json", "StopFailure", "rate_limit", None),
        (
            "notification.json",
            "Notification",
            "permission_prompt",
            "Claude needs your permission to use Bash",
        ),
        ("session_end.json", "SessionEnd", "logout", None),
    ],
)
def test_each_claude_event_discriminates_on_its_own_field(
    name: str, event: str, reason: str, detail: str | None
) -> None:
    """The corrected vocabulary: `error`, `notification_type`, `reason` -- never `error_type`
    or `end_reason`, the two assumed names that once made `limit_reached` unreachable in
    silence (the `_DISCRIMINATING_FIELDS` comment carries the full account). `SessionEnd`
    is retired (DEC-051) but its `reason` must still parse correctly on the way to being
    dropped, for hosts that have not re-run `install-agent-hooks`.
    """
    observed = _claude(_fixture(name))

    assert observed is not None
    assert (observed.event, observed.reason, observed.detail) == (event, reason, detail)


def test_a_claude_detail_line_is_bounded_and_single_lined() -> None:
    """One line, bounded, or nothing -- the budget both ends of the spool agree on."""
    long_message = "first line\n\tsecond   line " + "x" * MAXIMUM_DETAIL_CHARACTERS
    observed = _claude({**_fixture("stop.json"), "last_assistant_message": long_message})

    assert observed is not None
    assert observed.detail is not None
    assert len(observed.detail) == MAXIMUM_DETAIL_CHARACTERS
    assert observed.detail.startswith("first line second line x")
    assert "\n" not in observed.detail and "\t" not in observed.detail


def test_message_outranks_last_assistant_message_when_both_arrive() -> None:
    """`_DETAIL_FIELDS` is an ordered group: `message` is the event's own wording and wins
    over the trailing assistant line if an upstream payload ever carries both.
    """
    observed = _claude({**_fixture("notification.json"), "last_assistant_message": "trailing"})

    assert observed is not None
    assert observed.detail == "Claude needs your permission to use Bash"


def test_a_punctuated_reason_degrades_to_none_rather_than_reaching_disk() -> None:
    """A discriminating field is an enumerated token, not a sentence: anything outside the
    unpunctuated documented form is refused by `_plain_token`, and the record still spools
    -- degraded, never dropped.
    """
    observed = _claude({**_fixture("stop_failure.json"), "error": "rate limit: try later!"})

    assert observed is not None and observed.event == "StopFailure"
    assert observed.reason is None


def test_claude_admits_events_by_shape_not_by_allow_list() -> None:
    """The claude branch has no event allow-list -- deliberately, unlike Codex's: reading
    the discriminating fields as a group keeps an event added upstream from silently
    losing its record, and the drain decides what to interpret.
    """
    observed = _claude({**_fixture("stop.json"), "hook_event_name": "SomeFutureEvent"})

    assert observed is not None
    assert observed.event == "SomeFutureEvent"


def test_no_claude_payload_field_naming_a_path_prompt_or_foreign_id_reaches_disk() -> None:
    """The retention bound, asserted on the serialized record rather than on a field: the
    transcript path and working directory the payload carries would leak filesystem layout
    into a Telegram message, so they never leave the hook -- and neither does the
    provider's own session id or prompt id. A future field added upstream fails here
    without anyone having to predict its name.
    """
    forbidden = (
        "/home/owner/secret-project",
        "/home/owner/.claude/projects/secret-project/rollout-secret.jsonl",
        "provider-session-not-ours",
        "prompt-not-ours",
    )
    for name in ("stop.json", "stop_failure.json", "notification.json", "session_end.json"):
        payload = _fixture(name)
        observed = _claude(payload)
        assert observed is not None
        rendered = json.dumps(observed.document())
        for secret in forbidden:
            assert secret not in rendered, (
                f"{secret!r} reached the spool from {payload['hook_event_name']}"
            )
