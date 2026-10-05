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

from remote_agents.adapters.telegram.rollover_notifications import rollover_message
from remote_agents.application import rollover as rollover_module
from remote_agents.application.rollover import RolloverReport
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
