"""OpenCode's spool branch: two events admitted, one field read, everything else refused.

The same shape `test_codex_quirks.py` established, for the same reason — every fixture here is
deliberately over-filled with the fields
`docs/acceptance-2026-09-06-opencode-activity.md` refuses, so a case fails the day the parser
widens rather than the day somebody notices a command in a notification.

**Why the spool refuses fields the plugin already refused.** The plugin narrows at the source
and never emits them; this is the far end of a pipe, reading a file some other process wrote. It
is a different process, a different language and a different trust boundary, and a hand-edited
plugin in an operator's config directory is exactly the writer that would make the two disagree.

One asymmetry with Codex worth naming, because it is permanent rather than pending:
`session.idle` carries **no agent text and no field that could carry any**, so an OpenCode
`completed` never has a detail to expand. That is a property of the event, not a parser waiting
to be widened, and `_OPENCODE_DETAIL_FIELDS` is empty because there is nothing to put in it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from remote_agents.adapters.agents import activity_spool as spool
from remote_agents.adapters.agents.activity_spool import _observed_event
from remote_agents.adapters.agents.opencode.hooks import INSTALLED_EVENTS

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "opencode"

#: Every synthetic string the over-filled fixture carries that names a command, a path, a prompt
#: or a provider identifier. None of them may reach the record that lands on disk.
_FORBIDDEN = (
    "rm -rf /home/owner/secret-project",
    "rm *",
    "/home/owner/secret-project",
    "/home/owner/.local/share/opencode/opencode.db",
    "Do you want to allow deleting",
    "I am about to delete",
    "ses_synthetic000000000000000",
    "per_synthetic000000000000000",
    "msg_synthetic000000000000000",
    "call_synthetic00000000000000",
)


def _fixture(name: str) -> dict:
    document = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return {key: value for key, value in document.items() if not key.startswith("_")}


def _opencode(payload: dict) -> object:
    return _observed_event(
        BytesIO(json.dumps(payload).encode("utf-8")), "session", datetime.now(UTC), "opencode"
    )


def test_a_session_idle_is_a_completion_with_nothing_to_expand() -> None:
    """The event's occurrence is the whole signal, and always will be from this source."""
    observed = _opencode({"hook_event_name": "session.idle"})

    assert observed is not None
    assert observed.event == "session.idle"
    assert observed.detail is None
    assert observed.ask is None
    assert observed.reason is None


def test_a_permission_asked_names_the_ask_and_carries_no_agent_words() -> None:
    """`permission` is a tool class, so it lands in `ask` and never in `detail` (DEC-074)."""
    observed = _opencode(_fixture("overfilled_spool_payload.json"))

    assert observed is not None
    assert observed.event == "permission.asked"
    assert observed.ask == "bash"
    assert observed.detail is None


def test_no_overfilled_field_naming_a_command_path_or_prompt_reaches_disk() -> None:
    """Asserted on the serialized record, so a field added upstream fails without being named."""
    observed = _opencode(_fixture("overfilled_spool_payload.json"))

    assert observed is not None
    rendered = json.dumps(observed.document())
    for secret in _FORBIDDEN:
        assert secret not in rendered, f"{secret!r} reached the spool from permission.asked"


def test_an_overfilled_session_idle_still_contributes_nothing_but_its_occurrence() -> None:
    """The detail fields Claude and Codex read are refused here, by event as well as by name."""
    overfilled = {**_fixture("overfilled_spool_payload.json"), "hook_event_name": "session.idle"}

    observed = _opencode(overfilled)

    assert observed is not None
    assert observed.detail is None
    assert observed.ask is None, "an ask on a finished session would be a claim nothing made"
    rendered = json.dumps(observed.document())
    for secret in _FORBIDDEN:
        assert secret not in rendered


def test_the_opencode_field_allow_lists_are_exactly_what_the_measurement_licensed() -> None:
    """The allow-lists are pinned, not merely their consequences.

    The same pin `test_codex_quirks.py` carries, and for the reason a surviving mutant taught
    there: a field added to an allow-list can pass every behavioral test in a file, because
    `_first` stops at the first field that reads and `_plain_token` rejects most dangerous
    values anyway. Both are real defences and neither is the decision. The decision is what the
    measurement licensed, and a decision nothing asserts is one the next edit makes silently.
    """
    assert spool._OPENCODE_DETAIL_FIELDS == {}
    assert spool._OPENCODE_ASK_FIELDS == {"permission.asked": ("permission",)}

    licensed = {name for fields in spool._OPENCODE_ASK_FIELDS.values() for name in fields}
    for refused in ("patterns", "metadata", "always", "tool", "id", "sessionID", "message"):
        assert refused not in licensed, f"{refused} is not licensed by the measurement"


def test_the_admitted_events_are_exactly_the_ones_the_provider_declares() -> None:
    """The spool and the installed-event declaration are the same two names, or one is wrong."""
    assert spool._OPENCODE_EVENTS == frozenset(INSTALLED_EVENTS)


def test_every_other_event_type_is_refused_at_the_spool_too() -> None:
    """The stream carried 14 types; the plugin drops 12 and the spool must not re-admit them."""
    for event in (
        "permission.replied",
        "plugin.added",
        "message.part.updated",
        "session.updated",
        "Stop",
        "PermissionRequest",
        "Notification",
    ):
        assert _opencode({"hook_event_name": event, "permission": "bash"}) is None


def test_an_ask_that_is_not_a_plain_token_is_dropped_rather_than_rendered() -> None:
    """One sample, one value: the value space is unverified, so the narrow reader is the guard."""
    for value in ("rm -rf /tmp", "a quoted 'thing'", "bash; echo", 7, None, ["bash"]):
        observed = _opencode({"hook_event_name": "permission.asked", "permission": value})
        assert observed is not None, "an unreadable class must not cost the notification"
        assert observed.ask is None


def test_a_permission_asked_without_the_field_is_still_spooled() -> None:
    """A payload shape this build has not seen degrades to no ask, never to no record."""
    observed = _opencode({"hook_event_name": "permission.asked"})

    assert observed is not None and observed.event == "permission.asked"
    assert observed.ask is None


def test_claude_and_codex_parsing_are_untouched_by_the_opencode_branch() -> None:
    """Three providers share this function; adding the third changed neither of the first two."""
    claude = _observed_event(
        BytesIO(json.dumps({"hook_event_name": "Stop", "message": "claude words"}).encode()),
        "session",
        datetime.now(UTC),
        "claude",
    )
    assert claude is not None and claude.detail == "claude words"

    codex = _observed_event(
        BytesIO(json.dumps({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}).encode()),
        "session",
        datetime.now(UTC),
        "codex",
    )
    assert codex is not None and codex.ask == "Bash"

    # And the opencode names are not admitted for the other two, which share the parser.
    assert (
        _observed_event(
            BytesIO(json.dumps({"hook_event_name": "session.idle"}).encode()),
            "session",
            datetime.now(UTC),
            "codex",
        )
        is None
    )
