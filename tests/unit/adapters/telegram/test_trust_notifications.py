"""The trust question as a message, and the message it becomes once answered.

Rendered pure, and handed already-minted tokens, for the reason `render_activity` is: a
renderer that reached for a store would put the part worth testing exhaustively behind one
that is not.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from remote_agents.adapters.telegram.trust_notifications import (
    render_trust_question,
    render_trust_settled,
)
from remote_agents.application.session_actions import state_word
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

# Opaque `c1_` tokens, because `_validate_callback` refuses anything else — the renderers are
# handed already-minted tokens and never mint their own (DEC-011).
_TRUST = "c1_" + "t" * 24
_DECLINE = "c1_" + "d" * 24
_OPEN = "c1_" + "o" * 24


def _record(state: SessionState = SessionState.UNTRUSTED, profile: str = "claude") -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId(profile),
        SessionDisplayIdentity("editor", profile, "regular", 7),
        state,
        datetime.now(UTC),
    )


def _labels(message) -> list[str]:
    return [button.text for row in message.keyboard for button in row]


def test_an_answerable_agent_gets_both_answers_side_by_side_on_one_row() -> None:
    """Two wide, which supersedes DEC-032's one-answer-per-row clause (owner, 2026-09-11).

    This used to assert `[1, 1]`, on the reasoning that a pair of answers drawn two-wide would
    read as a pair of stops. The owner asked for the pair to share a row anyway: the two
    answers to one question are one choice, and stacking them made the message taller than the
    question it asks. DEC-032's other half is untouched — the stop row keeps its own two-wide
    row, and no stop is ever drawn on this message.
    """
    message = render_trust_question(_record(), answerable=True, trust=_TRUST, decline=_DECLINE)

    assert [len(row) for row in message.keyboard] == [2]
    assert _labels(message) == ["✅ Trust this project", "⛔ Don't trust — close it"]


def test_an_agent_whose_dialog_cannot_be_read_gets_only_the_no() -> None:
    """One answer is still one row, one wide — the pair shares a row, a lone button has none
    to share it with."""
    message = render_trust_question(_record(profile="codex"), answerable=False, decline=_DECLINE)

    assert [len(row) for row in message.keyboard] == [1]
    assert _labels(message) == ["⛔ Don't trust — close it"]


def test_the_question_names_the_session_and_says_nothing_runs_until_it_is_answered() -> None:
    message = render_trust_question(_record(), answerable=True, trust=_TRUST, decline=_DECLINE)

    assert "🔒" in message.text
    assert "editor" in message.text and "#7" in message.text
    assert "Nothing runs until you answer." in message.text


def test_the_question_carries_no_navigation_bar() -> None:
    """DEC-032: a notification is a message, not a screen, so it is barless by construction.

    Asserted on the drawn keyboard rather than on which function was called, because the bar
    is appended at one choke point and the only way to be sure it was not is to look.
    """
    message = render_trust_question(_record(), answerable=True, trust=_TRUST, decline=_DECLINE)

    assert not any(label in {"Sessions", "Launch", "Resume", "Back"} for label in _labels(message))


def test_a_trusted_session_amends_to_a_message_that_still_carries_a_keyboard() -> None:
    """DEC-034: an amendment carries its keyboard, or the owner is left with a dead message."""
    message = render_trust_settled(_record(SessionState.RUNNING), open_session=_OPEN)

    assert "Trusted" in message.text
    assert _labels(message) == ["Open session"]


def test_a_declined_session_amends_to_a_message_with_nothing_left_to_press() -> None:
    message = render_trust_settled(_record(SessionState.ENDED), open_session=_OPEN)

    assert "Closed without trusting." in message.text
    assert message.keyboard == ()


def test_the_settled_render_carries_no_navigation_bar_either() -> None:
    for state in (SessionState.RUNNING, SessionState.ENDED):
        message = render_trust_settled(_record(state), open_session=_OPEN)

        assert not any(
            label in {"Sessions", "Launch", "Resume", "Back"} for label in _labels(message)
        )


def test_an_answerable_question_without_a_trust_token_is_a_programming_error() -> None:
    """The two arguments have to agree, and disagreeing silently would draw a dead button."""
    with pytest.raises(ValueError):
        render_trust_question(_record(), answerable=True, decline=_DECLINE)


def test_an_owner_supplied_label_cannot_carry_markup_into_the_message() -> None:
    """The message is sent with `parse_mode=HTML` and the label is the owner's own words.

    Every caller of `_message` escapes; moving the wording into a shared renderer moved that
    responsibility here, and it was dropped in the move. A label containing a tag does not
    merely render oddly — Telegram refuses the send outright, which then feeds the refusal
    counter and can abandon the question entirely.
    """
    record = replace(
        _record(),
        display=SessionDisplayIdentity("editor", "claude", "regular", 7, "Tom & <b>Jerry</b>"),
    )

    message = render_trust_question(record, answerable=False, decline=_DECLINE)

    assert "<b>Jerry</b>" not in message.text
    assert "&lt;b&gt;Jerry&lt;/b&gt;" in message.text
    assert "Tom &amp; " in message.text


def test_a_settled_session_that_is_not_running_is_not_called_running() -> None:
    """A session can leave UNTRUSTED for more than two destinations.

    Its pane can die (FAILED, PRESERVED) or reconciliation can orphan it. Branching on "still
    listed" alone called every one of those "Trusted. The agent is running.", which asserts a
    fact the record does not carry — and for a force-stopped session it would have said the
    opposite of what happened.
    """
    for state in (SessionState.FAILED, SessionState.PRESERVED, SessionState.ORPHANED):
        message = render_trust_settled(_record(state), open_session=_OPEN)

        assert "The agent is running" not in message.text, state
        assert state_word(state, None) in message.text, state
