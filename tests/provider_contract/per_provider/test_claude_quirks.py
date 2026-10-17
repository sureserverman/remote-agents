"""Claude's discriminating behavior, driven from the requirement-built fixtures.

Unlike Codex's, most of these fixtures were *not* rebuilt from a capture document; the
exception is `notification_agent_needs_input.json`, whose field set was captured on 2.1.296
(`docs/acceptance-2026-10-09-claude-agent-needs-input.md`, BL-048). For the rest, the field
vocabulary is the one Claude's hook licence declares -- `_RECORD`'s discriminating and detail
fields and `HOOK_LICENCE` in `src/remote_agents/adapters/agents/claude/hooks.py`, whose own
comment records the
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
from remote_agents.adapters.agents.claude.hooks import HOOK_LICENCE
from remote_agents.ports.agent_activity import MAXIMUM_DETAIL_CHARACTERS

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _claude(payload: dict) -> object:
    return _observed_event(
        BytesIO(json.dumps(payload).encode("utf-8")), "session", datetime.now(UTC), HOOK_LICENCE
    )


def test_a_claude_stop_carries_the_agents_own_last_line() -> None:
    """`last_assistant_message` is the allowance DEC-013 clause (2) has always granted claude."""
    observed = _claude(_fixture("stop.json"))

    assert observed is not None
    assert observed.event == "Stop"
    assert observed.reason is None
    assert observed.detail == "Ran the suite and pushed the branch."


#: One entry as Claude Code 2.1.302 wrote it, measured 2026-10-17 with `sleep 40; echo done`
#: started in the background; the values here are synthetic.
_RUNNING_SHELL = {
    "id": "b-task",
    "type": "shell",
    "status": "running",
    "description": "a secret description",
    "command": "sleep 40 && cat /home/owner/secret",
}


def test_a_stop_that_waits_on_background_work_is_recorded_as_paused() -> None:
    """A turn that ended with a shell or subagent still in flight usually wakes again when it
    reports back, so its record says `paused` and the service holds it back (DEC-129)."""
    for task in (_RUNNING_SHELL, {"id": "a-task", "type": "subagent", "status": "running"}):
        observed = _claude({**_fixture("stop.json"), "background_tasks": [task]})
        assert observed is not None and observed.paused, observed
        assert "secret" not in json.dumps(observed.document()), observed.document()


def test_a_stop_with_nothing_in_flight_is_finished() -> None:
    observed = _claude({**_fixture("stop.json"), "background_tasks": []})
    assert observed is not None and not observed.paused


@pytest.mark.parametrize("value", [None, "shell", {"id": "b"}])
def test_a_stop_without_a_readable_list_is_finished(value: object) -> None:
    """An older Claude sends no list, so it cannot say it is waiting: its `Stop` is an end.
    A value that is not a list is read the same way."""
    payload = dict(_fixture("stop.json"))
    if value is None:
        del payload["background_tasks"]
    else:
        payload["background_tasks"] = value
    observed = _claude(payload)
    assert observed is not None and not observed.paused


def test_a_scheduled_wakeup_does_not_pause_the_stop() -> None:
    """A looping session waits on its schedule between iterations, and has finished each one."""
    cron = {"id": "c", "schedule": "*/5 * * * *", "recurring": True, "prompt": "check"}
    observed = _claude({**_fixture("stop.json"), "session_crons": [cron]})
    assert observed is not None and not observed.paused


def test_background_work_pauses_only_the_stop() -> None:
    """An ask while a shell runs in the background is still an ask."""
    payload = {**_fixture("notification.json"), "background_tasks": [_RUNNING_SHELL]}
    observed = _claude(payload)
    assert observed is not None and not observed.paused


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
    silence (the comment on Claude's `_RECORD` carries the full account). `SessionEnd`
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
    """Claude's `detail_fields` are an ordered group: `message` is the event's own wording and wins
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
        "/home/owner/.claude/scratchpad/secret-project",
    )
    for name in (
        "stop.json",
        "stop_failure.json",
        "notification.json",
        "notification_agent_needs_input.json",
        "session_end.json",
    ):
        payload = _fixture(name)
        observed = _claude(payload)
        assert observed is not None
        rendered = json.dumps(observed.document())
        for secret in forbidden:
            assert secret not in rendered, (
                f"{secret!r} reached the spool from {payload['hook_event_name']}"
            )


# --- the notification types the spool parses a message out of ------------------------------
#
# Named for what these cases actually check, which is the SPOOL's parsing, not the drain's
# routing. Since DEC-098 (2026-09-19) only `agent_needs_input` still becomes a `needs_answer`:
# `permission_prompt` left `_NOTIFICATIONS` because the `PermissionRequest` event now reports
# the same ask with words. The spool still reads both, and these cases still pin that -- a
# record this end parses correctly on the way to being dropped is the behaviour
# Claude's `_RECORD` comment argues for.


def test_a_permission_prompt_carries_the_message_claude_sends_with_it() -> None:
    """The measured half, and the one 59 of the owner's 78 rows came from.

    `permission_prompt`'s `message` was captured live on 2026-09-06 from this project's own
    spool — "Claude needs your permission to use Bash" — four times in one session.
    """
    observed = _claude(_fixture("notification.json"))

    assert observed is not None
    assert observed.reason == "permission_prompt"
    assert observed.detail == "Claude needs your permission to use Bash"


def test_a_needs_input_notification_carries_the_message_claude_sends_with_it() -> None:
    """MEASURED on 2026-10-10 (`docs/acceptance-2026-10-09-claude-agent-needs-input.md`, 2.1.296).

    The `claude agents` view fires this when a background session it lists changes to blocked,
    and the payload carries `message`: `"<label> needs your input: <needs>"`. So the record's
    detail is that line. This test used to pin `detail is None`, on a deduction from 19
    detail-less rows that the capture did not bear out for this build (BL-048).
    """
    payload = _fixture("notification_agent_needs_input.json")

    observed = _claude(payload)

    assert observed is not None
    assert observed.reason == "agent_needs_input"
    assert observed.detail == payload["message"]
    assert observed.ask is None


def test_a_mapped_notification_without_a_message_is_still_a_needs_answer() -> None:
    """The shape the old fixture pinned, kept as a shape and no longer claimed as what Claude sends.

    The 19 detail-less `needs_answer` rows of 2026-08-20 and 2026-09-06 stay unexplained: the
    measured `agent_needs_input` carries a message. Whatever produced them, a mapped notification
    with no `message` still becomes a record, with `detail=None` rather than invented words.
    """
    payload = {
        key: value
        for key, value in _fixture("notification_agent_needs_input.json").items()
        if key != "message"
    }

    observed = _claude(payload)

    assert observed is not None
    assert observed.reason == "agent_needs_input"
    assert observed.detail is None, "nothing invents words the payload did not carry"
