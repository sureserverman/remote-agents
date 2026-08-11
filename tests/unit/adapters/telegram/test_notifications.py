from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from remote_agents.adapters.telegram.notifications import (
    OPEN_SESSION_LABEL,
    render_activity,
)
from remote_agents.adapters.telegram.presenters import MAX_TELEGRAM_TEXT_UNITS
from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
)

OPEN = "c1_open_session_token"
DISPLAY = "atlas · claude · fresh · #4"
OBSERVED = datetime(2026, 8, 11, 14, 5, tzinfo=UTC)


def _activity(
    kind: ActivityKind,
    *,
    detail: str | None = None,
    confidence: ActivityConfidence = ActivityConfidence.REPORTED,
    observed_at: datetime = OBSERVED,
) -> AgentActivity:
    return AgentActivity(
        session_id="0191f2c2-0000-7000-8000-00000000abcd",
        kind=kind,
        detail=detail,
        observed_at=observed_at,
        confidence=confidence,
    )


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


EVERY_KIND = (
    ActivityKind.COMPLETED,
    ActivityKind.LIMIT_REACHED,
    ActivityKind.NEEDS_ANSWER,
    ActivityKind.ENDED,
    ActivityKind.QUIET,
)


@pytest.mark.parametrize("kind", EVERY_KIND)
def test_every_kind_names_the_session_and_offers_to_open_it(kind: ActivityKind) -> None:
    confidence = (
        ActivityConfidence.INFERRED if kind is ActivityKind.QUIET else ActivityConfidence.REPORTED
    )
    message = render_activity(
        _activity(kind, confidence=confidence), display=DISPLAY, open_session=OPEN
    )

    assert DISPLAY in message.text
    assert message.keyboard == ((message.keyboard[0][0],),)
    assert message.keyboard[0][0].text == OPEN_SESSION_LABEL
    assert message.keyboard[0][0].callback_data == OPEN


def test_every_kind_says_something_distinct() -> None:
    """No two kinds may render the same sentence — a mapping that collapses tells the owner
    one thing while the service knows another."""
    rendered = {
        other: render_activity(
            _activity(
                other,
                confidence=(
                    ActivityConfidence.INFERRED
                    if other is ActivityKind.QUIET
                    else ActivityConfidence.REPORTED
                ),
            ),
            display=DISPLAY,
            open_session=OPEN,
        ).text
        for other in EVERY_KIND
    }
    assert len(set(rendered.values())) == len(EVERY_KIND)
    assert all(text.strip() for text in rendered.values())


def test_a_completed_session_carries_what_the_agent_said() -> None:
    message = render_activity(
        _activity(ActivityKind.COMPLETED, detail="Refactored the parser and ran the suite."),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "Refactored the parser and ran the suite." in message.text


def test_detail_is_escaped_rather_than_rendered_as_markup() -> None:
    message = render_activity(
        _activity(ActivityKind.COMPLETED, detail="<b>bold</b> & <script>alert(1)</script>"),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "<script>" not in message.text
    assert "&lt;script&gt;" in message.text
    assert "&amp;" in message.text


def test_a_display_identity_carrying_markup_is_escaped_too() -> None:
    message = render_activity(
        _activity(ActivityKind.ENDED),
        display="<i>project</i> · claude",
        open_session=OPEN,
    )
    assert "<i>project</i>" not in message.text
    assert "&lt;i&gt;project&lt;/i&gt;" in message.text


def test_an_unbounded_detail_still_fits_the_telegram_budget() -> None:
    """The application layer bounds detail to 240 characters, and this bounds it again — the
    renderer is not entitled to assume the only caller it has today."""
    message = render_activity(
        _activity(ActivityKind.COMPLETED, detail="x" * 20_000),
        display="y" * 20_000,
        open_session=OPEN,
    )
    assert _utf16_units(message.text) <= MAX_TELEGRAM_TEXT_UNITS


def test_an_inferred_need_for_an_answer_is_worded_as_a_possibility() -> None:
    reported = render_activity(
        _activity(ActivityKind.NEEDS_ANSWER, confidence=ActivityConfidence.REPORTED),
        display=DISPLAY,
        open_session=OPEN,
    )
    inferred = render_activity(
        _activity(ActivityKind.NEEDS_ANSWER, confidence=ActivityConfidence.INFERRED),
        display=DISPLAY,
        open_session=OPEN,
    )

    assert reported.text != inferred.text
    assert "is waiting" in reported.text
    assert "may be waiting" in inferred.text
    assert "may be waiting" not in reported.text


def test_quiet_is_a_report_of_silence_and_never_a_claim_of_completion() -> None:
    """The Stage 2 gate's judgment criterion, pinned: the heuristic describes what was
    observed — no output — and never the conclusion the owner might jump to."""
    message = render_activity(
        _activity(ActivityKind.QUIET, confidence=ActivityConfidence.INFERRED),
        display=DISPLAY,
        open_session=OPEN,
    )
    lowered = message.text.casefold()
    assert "no output since" in lowered
    assert "finished" not in lowered
    assert "completed" not in lowered
    assert "done" not in lowered


def test_quiet_names_the_time_it_stopped_being_observed_to_change() -> None:
    message = render_activity(
        _activity(ActivityKind.QUIET, confidence=ActivityConfidence.INFERRED),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "14:05 UTC" in message.text


def test_quiet_renders_the_same_moment_whatever_offset_it_arrived_in() -> None:
    """An observation is an instant; two spellings of one instant must not read as two."""
    elsewhere = OBSERVED.astimezone(timezone(timedelta(hours=5, minutes=30)))
    message = render_activity(
        _activity(
            ActivityKind.QUIET,
            confidence=ActivityConfidence.INFERRED,
            observed_at=elsewhere,
        ),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "14:05 UTC" in message.text


def test_quiet_never_renders_agent_text_even_if_a_caller_supplies_it() -> None:
    """Nothing said this. A quiet report that carried a parting sentence would present the
    last thing on the screen as a statement the agent chose to make."""
    message = render_activity(
        _activity(
            ActivityKind.QUIET,
            detail="I have completed the migration.",
            confidence=ActivityConfidence.INFERRED,
        ),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "migration" not in message.text


def test_an_inferred_report_says_so_rather_than_asserting_it() -> None:
    for kind in (ActivityKind.QUIET, ActivityKind.NEEDS_ANSWER):
        message = render_activity(
            _activity(kind, confidence=ActivityConfidence.INFERRED),
            display=DISPLAY,
            open_session=OPEN,
        )
        assert "not something it reported" in message.text


def test_a_callback_that_is_not_an_opaque_token_is_refused() -> None:
    """A notification's one button is the only thing it can do; a payload that is not a
    server-side token would put application meaning into Telegram's hands."""
    for rejected in ("session.detail:42", "", "c1_" + "x" * 100, "c1_ünicode"):
        with pytest.raises(ValueError):
            render_activity(
                _activity(ActivityKind.COMPLETED), display=DISPLAY, open_session=rejected
            )
