from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.notifications import (
    OPEN_SESSION_LABEL,
    ActivityNotifier,
    SessionGroup,
    grouped_for_delivery,
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
    ActivityKind.OUTPUT_LIMIT,
    ActivityKind.NEEDS_ANSWER,
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
        _activity(ActivityKind.COMPLETED),
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


def test_a_need_for_an_answer_is_stated_plainly_because_the_agent_asked() -> None:
    """The only sources left are a permission prompt and an agent saying it needs input.

    Both are the agent speaking, so the sentence no longer hedges. It used to, for the third
    source — an upstream sixty-second idle timer — which was retired rather than softened: a
    weakened sentence makes a weak signal read better, not become worth sending.
    """
    message = render_activity(
        _activity(ActivityKind.NEEDS_ANSWER, confidence=ActivityConfidence.REPORTED),
        display=DISPLAY,
        open_session=OPEN,
    )

    assert "The agent is waiting for an answer." in message.text
    assert "may be waiting" not in message.text
    assert "not something it reported" not in message.text


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
    """Pane quiet is the one guess left, and the hedge is what keeps it honest.

    The rule is the renderer's rather than the sentence's on purpose: it fires on the
    confidence, so a future inferred kind cannot arrive unhedged by whoever writes its wording.
    """
    message = render_activity(
        _activity(ActivityKind.QUIET, confidence=ActivityConfidence.INFERRED),
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


class _RecordingView:
    """The `LiveView` surface a notifier actually uses: one send, addressed to a chat."""

    chat_id = 11

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self._next_id = 900

    async def send_apart(self, _bot: object, arguments: dict[str, object]) -> int:
        self.sent.append(arguments)
        self._next_id += 1
        return self._next_id


class _SilentBot:
    async def edit_message_reply_markup(self, **_kwargs: object) -> None:
        return None


class _Clock:
    """A clock a test can move, so the rate limit's *expiry* is reachable at all."""

    def __init__(self) -> None:
        self.moment = datetime(2026, 8, 11, 14, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


def _notifier(clock: _Clock, *, rate_limit_seconds: float = 120.0):
    view = _RecordingView()

    async def display(_session_id: str) -> str:
        return DISPLAY

    notifier = ActivityNotifier(
        view=view,
        callbacks=CallbackStateStore(),
        owner_user_id=7,
        display=display,
        rate_limit_seconds=rate_limit_seconds,
        now=clock,
    )
    notifier.attach(_SilentBot())
    return notifier, view


async def test_the_same_kind_is_delivered_again_once_the_window_has_passed() -> None:
    """The suppression window has two halves and only one of them had a test.

    A rate limit that never expires is not a rate limit, it is a mute: the second time an
    agent finishes — an hour later, on a different task — the owner is owed that message.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    activity = _activity(ActivityKind.COMPLETED)

    assert await notifier.deliver([activity]) == 1
    assert await notifier.deliver([activity]) == 0, "the burst was not collapsed"

    clock.advance(121)
    assert await notifier.deliver([activity]) == 1
    assert len(view.sent) == 2


async def test_the_rate_limit_map_does_not_grow_for_the_life_of_the_service() -> None:
    """One entry per (session, kind) is small; unbounded over a service that launches
    sessions all day is not. An entry older than the window suppresses nothing."""
    clock = _Clock()
    notifier, _ = _notifier(clock)

    for index in range(25):
        await notifier.deliver(
            [
                AgentActivity(
                    session_id=f"session-{index}",
                    kind=ActivityKind.COMPLETED,
                    detail=None,
                    observed_at=clock.moment,
                )
            ]
        )
    assert len(notifier._last_sent) == 25

    # Past the window *and* the retention that keeps a lapsed entry's repeat count readable.
    clock.advance(241)
    await notifier.deliver([])

    assert notifier._last_sent == {}, "expired suppressions were kept for the life of the run"


async def test_an_entry_still_inside_its_window_is_not_forgotten() -> None:
    """The other direction, which the pruning could break silently: forgetting an entry that
    is still suppressing turns the burst collapse off without anything failing."""
    clock = _Clock()
    notifier, view = _notifier(clock)
    activity = _activity(ActivityKind.COMPLETED)

    assert await notifier.deliver([activity]) == 1
    clock.advance(60)
    assert await notifier.deliver([activity]) == 0
    assert len(view.sent) == 1


async def test_a_notification_the_owner_received_is_never_re_queued_as_undelivered() -> None:
    """The guard around the mint and the render is as wide as the send it follows.

    Drawn narrower, a failure between the send and the keyboard escaped as "held for retry"
    over a message the owner already had — and the next pass, finding the rate limit recorded,
    dropped it silently as a collapsed burst. Two false statements about one notification.
    """
    clock = _Clock()
    view = _RecordingView()

    class _RefusingCallbacks(CallbackStateStore):
        """Fails at the *mint*, which is the step the narrow guard left outside itself.

        Not the keyboard call: that one was guarded even before the repair, so a test driving
        it would pass against the defect it claims to cover.
        """

        def create(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("the callback store refused a token")

    async def display(_session_id: str) -> str:
        return DISPLAY

    notifier = ActivityNotifier(
        view=view,
        callbacks=_RefusingCallbacks(),
        owner_user_id=7,
        display=display,
        now=clock,
    )
    notifier.attach(_SilentBot())

    assert await notifier.deliver([_activity(ActivityKind.COMPLETED)]) == 1
    assert notifier.pending_count() == 0, "a delivered notification was held for retry"
    assert len(view.sent) == 1, "the owner was told once"

    # And the next pass must not report it a second time under a different wrong name.
    assert await notifier.deliver([]) == 0
    assert len(view.sent) == 1


def test_an_output_ceiling_is_not_worded_as_a_usage_limit() -> None:
    """Two facts, two next moves: a rate limit is waited out, an output ceiling is continued
    from. One sentence for both named the alarming one for the routine event."""
    usage = render_activity(
        _activity(ActivityKind.LIMIT_REACHED), display=DISPLAY, open_session=OPEN
    )
    output = render_activity(
        _activity(ActivityKind.OUTPUT_LIMIT), display=DISPLAY, open_session=OPEN
    )

    assert "usage limit" in usage.text
    assert "usage limit" not in output.text
    assert "output length limit" in output.text


def test_text_no_encoder_can_carry_is_replaced_rather_than_raising() -> None:
    """A lone surrogate is a legal `str` and an illegal encode.

    `json.loads` decodes `\\udXXX` in a spooled payload straight back into one, and the spool
    tolerates a foreign writer by design — so this arrived from outside, reached the UTF-16
    budget, and raised out of the middle of the render. Because it raised *before* the send,
    the activity was never popped: it sat at the head of the queue and every later
    notification, for every session, queued behind it silently.
    """
    message = render_activity(
        _activity(ActivityKind.COMPLETED, detail="done \ud800 here"),
        display="proj \udcff x",
        open_session=OPEN,
    )

    assert "�" in message.text
    assert message.text.encode("utf-8"), "the rendered message must be sendable"
    assert "done" in message.text and "proj" in message.text


async def test_a_standing_condition_backs_off_instead_of_repeating_every_window() -> None:
    """`needs_answer` repeats for as long as the owner does not answer.

    A fixed window turns that into a message every two minutes all night. The window doubles
    per consecutive repeat instead — the first message is as prompt as ever, and only the
    copies get rarer.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    waiting = _activity(ActivityKind.NEEDS_ANSWER)

    assert await notifier.deliver([waiting]) == 1
    clock.advance(121)
    assert await notifier.deliver([waiting]) == 1, "the second copy is still owed"

    # Now backed off to 240s: at 121s more it is still suppressed.
    clock.advance(121)
    assert await notifier.deliver([waiting]) == 0
    clock.advance(121)
    assert await notifier.deliver([waiting]) == 1
    assert len(view.sent) == 3


async def test_a_different_kind_from_the_same_session_starts_the_count_over() -> None:
    """A repeat count claims nothing has changed. A different kind is something changing."""
    clock = _Clock()
    notifier, view = _notifier(clock)
    waiting = _activity(ActivityKind.NEEDS_ANSWER)

    assert await notifier.deliver([waiting]) == 1
    clock.advance(121)
    assert await notifier.deliver([waiting]) == 1  # repeats == 1, window now 240s
    clock.advance(121)
    assert await notifier.deliver([_activity(ActivityKind.COMPLETED)]) == 1

    # The answer arrived and the agent moved on, so waiting again is news, not a repeat.
    clock.advance(121)
    assert await notifier.deliver([waiting]) == 1
    assert len(view.sent) == 4


async def test_a_backed_off_entry_is_not_forgotten_while_it_is_still_suppressing() -> None:
    """The pruning measures each entry against its own window.

    Under a fixed horizon the backed-off entries — the repeating ones, which are the only ones
    the backoff is for — were forgotten while still suppressing, quietly restoring the
    every-two-minutes behaviour for exactly the case it was added to fix.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    waiting = _activity(ActivityKind.NEEDS_ANSWER)

    await notifier.deliver([waiting])
    clock.advance(121)
    await notifier.deliver([waiting])  # window now 240s

    clock.advance(121)
    await notifier.deliver([])  # a pass that prunes but sends nothing

    assert len(notifier._last_sent) == 1, "an entry still suppressing was pruned"
    assert await notifier.deliver([waiting]) == 0
    assert len(view.sent) == 2


async def test_many_sessions_at_once_are_spread_across_passes_not_fired_at_the_chat() -> None:
    """The per-(session, kind) limit is per key, so it bounds one session and not the chat.

    Twenty sessions stopping together are twenty distinct keys, none suppressing any other.
    Each notification costs two Bot API calls, so an unbounded pass runs past Telegram's
    per-chat rate and the 429s come back as a growing backlog. Nothing is dropped here — the
    remainder waits for the next pass, seconds later.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    burst = [
        AgentActivity(
            session_id=f"session-{index}",
            kind=ActivityKind.COMPLETED,
            detail=None,
            observed_at=clock.moment,
        )
        for index in range(25)
    ]

    assert await notifier.deliver(burst) == 10
    assert notifier.pending_count() == 15, "the remainder must wait, not be discarded"

    assert await notifier.deliver([]) == 10
    assert await notifier.deliver([]) == 5
    assert notifier.pending_count() == 0
    assert len(view.sent) == 25, "every observation still reached the owner"


SESSION_A = "0191f2c2-0000-7000-8000-00000000aaaa"
SESSION_B = "0191f2c2-0000-7000-8000-00000000bbbb"


def _observed(
    session_id: str,
    kind: ActivityKind,
    *,
    detail: str | None = None,
    minute: int = 0,
) -> AgentActivity:
    confidence = (
        ActivityConfidence.INFERRED if kind is ActivityKind.QUIET else ActivityConfidence.REPORTED
    )
    return AgentActivity(
        session_id=session_id,
        kind=kind,
        detail=detail,
        observed_at=OBSERVED + timedelta(minutes=minute),
        confidence=confidence,
    )


def test_two_sessions_whose_observations_interleave_come_back_in_first_appearance_order() -> None:
    """The queue feeding this is FIFO and grouping must not quietly re-sort it.

    The session heard from first is told first, whatever its identifier is and whatever the
    clock says: here the session that speaks first carries the *latest* stamp in the batch, so
    a grouping that ordered by time across sessions would put it second.
    """
    groups = grouped_for_delivery(
        [
            _observed(SESSION_B, ActivityKind.COMPLETED, detail="pushed the branch", minute=9),
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="ran the suite", minute=1),
            _observed(SESSION_B, ActivityKind.NEEDS_ANSWER, detail="may I write here?", minute=2),
            _observed(SESSION_A, ActivityKind.QUIET, minute=3),
        ]
    )

    assert [group.session_id for group in groups] == [SESSION_B, SESSION_A]
    assert len(groups[0].activities) == 2
    assert len(groups[1].activities) == 2


def test_the_same_thing_said_twice_collapses_to_the_copy_carrying_the_newer_moment() -> None:
    """A `Stop` hook fires per turn, so one instruction reports "finished" several times over.

    Both reports are true and the owner needs one of them — the later, because it is the one
    that is still true when the message arrives.
    """
    groups = grouped_for_delivery(
        [
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="Ran the suite.", minute=1),
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="Ran the suite.", minute=6),
        ]
    )

    assert len(groups) == 1
    assert groups[0].activities == (
        _observed(SESSION_A, ActivityKind.COMPLETED, detail="Ran the suite.", minute=6),
    )


def test_the_same_kind_carrying_different_agent_text_is_two_things_the_agent_said() -> None:
    """Collapsing on the kind alone would delete one of them and say nothing about it."""
    groups = grouped_for_delivery(
        [
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="Ran the suite.", minute=1),
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="Pushed the branch.", minute=2),
        ]
    )

    assert len(groups) == 1
    assert [activity.detail for activity in groups[0].activities] == [
        "Ran the suite.",
        "Pushed the branch.",
    ]


def test_two_quiet_reports_collapse_even_though_neither_carries_any_agent_text() -> None:
    """`QUIET` always carries `detail=None`, so a pair of `None`s is the real duplicate case.

    It is the one that matters for the profiles watched by their panes: they have no hooks, so
    quiet is the only observation they ever produce, and a rule that treated "nothing said it"
    as unmatchable would collapse nothing for exactly those sessions.
    """
    groups = grouped_for_delivery(
        [
            _observed(SESSION_A, ActivityKind.QUIET, minute=1),
            _observed(SESSION_A, ActivityKind.QUIET, minute=4),
        ]
    )

    assert len(groups) == 1
    assert groups[0].activities == (_observed(SESSION_A, ActivityKind.QUIET, minute=4),)


def test_a_single_observation_becomes_a_group_of_one() -> None:
    """The ordinary case, and the one the delivery pass sees most: one session, one thing."""
    only = _observed(SESSION_A, ActivityKind.NEEDS_ANSWER, detail="which branch?", minute=2)

    assert grouped_for_delivery([only]) == (SessionGroup(SESSION_A, (only,)),)


def test_a_group_reads_in_the_order_its_observations_were_made() -> None:
    """One session's news is a small timeline, so it is told in the order it happened."""
    groups = grouped_for_delivery(
        [
            _observed(SESSION_A, ActivityKind.QUIET, minute=7),
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="ran the suite", minute=2),
            _observed(SESSION_A, ActivityKind.NEEDS_ANSWER, detail="which branch?", minute=5),
        ]
    )

    assert [activity.kind for activity in groups[0].activities] == [
        ActivityKind.COMPLETED,
        ActivityKind.NEEDS_ANSWER,
        ActivityKind.QUIET,
    ]


def test_a_collapsed_observation_takes_the_place_its_newest_copy_earned() -> None:
    """Collapse first, order second, because a survivor carries its newest stamp.

    Ordering first would leave the surviving line where its *earliest* copy sat, so a sentence
    stamped 14:20 would be printed above one stamped 14:10 — a timeline that runs backwards
    inside a single message.
    """
    groups = grouped_for_delivery(
        [
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="ran the suite", minute=1),
            _observed(SESSION_A, ActivityKind.NEEDS_ANSWER, detail="which branch?", minute=5),
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="ran the suite", minute=9),
        ]
    )

    assert [activity.kind for activity in groups[0].activities] == [
        ActivityKind.NEEDS_ANSWER,
        ActivityKind.COMPLETED,
    ]
