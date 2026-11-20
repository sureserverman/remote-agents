"""The failed-rollover notice (DEC-031, DEC-115): every cause the pass can record is said in
words, and the notice claims nothing it does not know -- "predecessor preserved" only where the
predecessor was kept, an open successor named where one was left open, no running time it did
not measure."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.telegram.rollover_notifications import _WHY, rollover_message
from remote_agents.application import rollover as rollover_module
from remote_agents.application.rollover import PERMISSIONS_FAILED, RolloverReport
from remote_agents.domain import rollover as domain_rollover
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports import terminal
from remote_agents.ports.handoff_envelopes import FAILURE_CODES
from remote_agents.ports.rollover_store import Rollover

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
_CODE = re.compile(r"[a-z]+(-[a-z]+)+\Z")


def _record(sequence: int) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("p"),
        ProfileId("claude"),
        SessionDisplayIdentity("p", "claude", "regular", sequence, None),
        SessionState.RUNNING,
        NOW,
    )


def _report(state: RolloverState, code: str | None, *, detail=None, successor=None):
    predecessor = _record(3)
    row = Rollover(
        id="r1",
        handoff_id="h-0123456789abcdef0123",
        predecessor_session_id=predecessor.session_id,
        successor_session_id=None if successor is None else successor.session_id,
        project_id=ProjectId("p"),
        profile_id=ProfileId("claude"),
        reason="workflow",
        plan=None,
        state=state,
        failure_code=code,
        failure_detail=detail,
        requested_at=NOW,
        updated_at=NOW,
    )
    return RolloverReport(row, predecessor, successor)


def _pass_failure_codes() -> set[str]:
    """Every code the pass records under FAILED: its own constants, the domain's, the plugin's."""
    source = Path(rollover_module.__file__).read_text(encoding="utf-8")
    own = {
        node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and _CODE.fullmatch(node.value.value)
    }
    domain = {domain_rollover.SUCCESSOR_UNKNOWN, domain_rollover.SUCCESSOR_FAILED}
    return (own | domain | set(FAILURE_CODES)) - {"stop-unconfirmed"}


#: Every code a STOP_FAILED row can carry: the pass's own two and every stop cause the terminal
#: can answer (`graceful_stop`'s details).
_STOP_CODES = {
    "predecessor-not-idle",
    "stop-unconfirmed",
    terminal.GRACEFUL_TIMEOUT,
    terminal.COMPOSER_HOLDS_TEXT,
    terminal.KEYS_BUSY,
    terminal.AGENT_ASKING,
    terminal.UNKNOWN_SESSION,
    terminal.NOT_IDLE,
}


def test_the_sweep_finds_the_pass_codes() -> None:
    assert {"not-typed", "adoption-timeout", "request-lost", "branch-mismatch"} <= (
        _pass_failure_codes()
    )


@pytest.mark.parametrize("code", sorted(_pass_failure_codes()))
def test_every_failure_code_is_said_in_words(code: str) -> None:
    text = rollover_message(_report(RolloverState.FAILED, code), project_name="remote-agents")

    assert text.startswith("Rollover failed: claude in remote-agents #3 — ")
    assert code not in text, "the code itself is not the owner's word for it"


@pytest.mark.parametrize("code", sorted(_STOP_CODES))
def test_every_stop_failure_is_said_in_words_and_claims_no_running_time(code: str) -> None:
    text = rollover_message(_report(RolloverState.STOP_FAILED, code), project_name="remote-agents")

    assert text.startswith("Rollover stop failed: claude in remote-agents #3 — ")
    assert code not in text
    assert "minutes" not in text, "no stop cause is a wait the notice measured"
    assert "both are running" not in text
    assert "force stop" in text


def test_a_predecessor_that_was_gone_is_not_said_to_be_preserved() -> None:
    text = rollover_message(
        _report(RolloverState.FAILED, "predecessor-gone"), project_name="remote-agents"
    )

    assert "preserved" not in text


@pytest.mark.parametrize(
    "code", ["not-typed", "successor-untrusted", "adoption-timeout", "branch-mismatch"]
)
def test_a_failure_that_left_the_successor_open_says_so(code: str) -> None:
    text = rollover_message(
        _report(RolloverState.FAILED, code, successor=_record(4)), project_name="remote-agents"
    )

    assert "predecessor preserved" in text
    assert "#4" in text and "still open" in text


@pytest.mark.parametrize("code", ["launch-failed", "successor-failed", "predecessor-not-idle"])
def test_a_failure_with_no_successor_left_names_none(code: str) -> None:
    text = rollover_message(_report(RolloverState.FAILED, code), project_name="remote-agents")

    assert "still open" not in text


def test_a_stop_that_may_have_landed_does_not_claim_the_predecessor_was_kept() -> None:
    text = rollover_message(
        _report(RolloverState.STOP_FAILED, "stop-unconfirmed"), project_name="remote-agents"
    )

    assert "preserved" not in text and "check" in text


def test_what_reaches_the_chat_is_escaped() -> None:
    text = rollover_message(
        _report(RolloverState.FAILED, "not-typed", detail="<b>dialog</b>"),
        project_name="a <i>project</i>",
    )

    assert "<b>" not in text and "<i>" not in text
    assert "&lt;b&gt;dialog" in text and "a &lt;i&gt;project" in text


@pytest.mark.parametrize(
    "state",
    [s for s in RolloverState if s not in (RolloverState.FAILED, RolloverState.STOP_FAILED)],
    ids=lambda state: state.value,
)
def test_no_other_end_or_move_is_told(state: RolloverState) -> None:
    assert rollover_message(_report(state, None), project_name="remote-agents") == ""


def test_an_untrusted_successor_is_said_to_have_waited_out_the_trust_patience() -> None:
    text = rollover_message(
        _report(RolloverState.FAILED, "successor-untrusted", successor=_record(4)),
        project_name="remote-agents",
    )

    assert "was not trusted within 2 hours" in text
    assert "waiting on its folder-trust question" not in text


# --- A successor whose permissions were not applied (OD-2). ---


def test_a_permission_failed_rollover_names_the_cause_and_what_is_left_open() -> None:
    text = rollover_message(
        _report(
            RolloverState.FAILED,
            PERMISSIONS_FAILED,
            detail="thread/settings/update refused <b>(code -32600)</b>",
            successor=_record(4),
        ),
        project_name="p",
    )

    assert "the successor's permissions could not be applied" in text
    assert "predecessor preserved" in text
    assert "Its successor #4 is still open" in text
    # The daemon's words are data, never markup.
    assert "&lt;b&gt;(code -32600)&lt;/b&gt;" in text
    assert "<b>" not in text


def test_a_permission_failed_rollover_is_not_shown_as_its_code() -> None:
    text = rollover_message(_report(RolloverState.FAILED, PERMISSIONS_FAILED), project_name="p")

    assert PERMISSIONS_FAILED not in text
    assert PERMISSIONS_FAILED in _WHY


def test_a_note_rollover_that_could_not_ask_names_the_session_and_no_successor() -> None:
    """A note rollover types into the predecessor, before any successor exists (BL-119)."""
    from dataclasses import replace

    from remote_agents.domain.rollover import RolloverKind

    report = _report(RolloverState.FAILED, "not-typed", detail="composing")
    report = RolloverReport(
        replace(report.rollover, kind=RolloverKind.NOTE, handoff_id=None, reason="owner"),
        report.predecessor,
        None,
    )

    text = rollover_message(report, project_name="remote-agents")

    assert "note" in text and "successor" not in text
    assert "predecessor preserved" in text


def test_a_note_that_was_never_written_is_said_with_its_patience() -> None:
    from remote_agents.application.rollover import NOTE_PATIENCE

    text = rollover_message(
        _report(RolloverState.FAILED, "note-unwritten"), project_name="remote-agents"
    )

    assert f"{int(NOTE_PATIENCE.total_seconds() // 60)} minutes" in text
    assert "note" in text


def test_a_note_rollover_whose_successor_was_not_typed_to_names_the_successor() -> None:
    from dataclasses import replace

    from remote_agents.domain.rollover import RolloverKind

    report = _report(RolloverState.FAILED, "not-typed", detail="composing", successor=_record(4))
    report = RolloverReport(
        replace(report.rollover, kind=RolloverKind.NOTE, handoff_id=None, reason="owner"),
        report.predecessor,
        report.successor,
    )

    text = rollover_message(report, project_name="remote-agents")

    assert "successor" in text and "handoff note was not confirmed" not in text
    assert "#4" in text and "still open" in text


def test_a_successor_held_on_a_hook_review_is_named_in_its_notice() -> None:
    """Sub-plan 5 Task 3.2 (BL-146): the patience ran out on a dialog only the owner answers."""
    text = rollover_message(
        _report(
            RolloverState.FAILED,
            "successor-untrusted",
            detail="a hook review",
            successor=_record(4),
        ),
        project_name="opaque-editor",
    )

    assert "a hook review" in text, text
    assert "answer it in its pane or stop it, then ask again" in text, text
    assert "2 hours after it was first held" in text, text
    assert "predecessor preserved" in text, text


def test_a_successor_never_trusted_keeps_the_folder_trust_words() -> None:
    text = rollover_message(
        _report(RolloverState.FAILED, "successor-untrusted", successor=_record(4)),
        project_name="opaque-editor",
    )

    assert "was not trusted within 2 hours" in text, text
    assert "hook review" not in text


def test_a_stale_note_rollover_never_claims_nothing_was_asked() -> None:
    """`request-stale` covers a press never typed and a note written but found too late, where
    the request had been typed (Stage 4 gate, 2026-11-20): the push may not say nothing was
    asked, and it says no session was started."""
    text = rollover_message(
        _report(RolloverState.FAILED, "request-stale"), project_name="remote-agents"
    )

    assert "asked nothing" not in text and "nothing was" not in text
    assert "no fresh session was started" in text
    assert "note" in text


def test_a_stale_note_rollover_row_names_the_press_or_its_note() -> None:
    from remote_agents.application.session_views import _rollover_failure_reason

    words = _rollover_failure_reason("request-stale")

    assert "before the session was asked" not in words
    assert "note" in words
