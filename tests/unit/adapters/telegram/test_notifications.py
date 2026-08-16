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
REPORTED = ActivityConfidence.REPORTED


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


def _group(*activities: AgentActivity) -> SessionGroup:
    """One session's news, for a renderer that no longer takes a lone observation.

    Most of this file's cases are about one observation's wording and are unaffected by
    grouping, so they wrap it here rather than each building a bundle inline.
    """
    return SessionGroup(activities[0].session_id, activities)


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
        _group(_activity(kind, confidence=confidence)), display=DISPLAY, open_session=OPEN
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
            _group(_activity(
                other,
                confidence=(
                    ActivityConfidence.INFERRED
                    if other is ActivityKind.QUIET
                    else ActivityConfidence.REPORTED
                ),
            )),
            display=DISPLAY,
            open_session=OPEN,
        ).text
        for other in EVERY_KIND
    }
    assert len(set(rendered.values())) == len(EVERY_KIND)
    assert all(text.strip() for text in rendered.values())


def test_a_completed_session_carries_what_the_agent_said() -> None:
    message = render_activity(
        _group(
            _activity(ActivityKind.COMPLETED, detail="Refactored the parser and ran the suite.")
        ),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "Refactored the parser and ran the suite." in message.text


def test_detail_is_escaped_rather_than_rendered_as_markup() -> None:
    message = render_activity(
        _group(_activity(ActivityKind.COMPLETED, detail="<b>bold</b> & <script>alert(1)</script>")),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "<script>" not in message.text
    assert "&lt;script&gt;" in message.text
    assert "&amp;" in message.text


def test_a_display_identity_carrying_markup_is_escaped_too() -> None:
    message = render_activity(
        _group(_activity(ActivityKind.COMPLETED)),
        display="<i>project</i> · claude",
        open_session=OPEN,
    )
    assert "<i>project</i>" not in message.text
    assert "&lt;i&gt;project&lt;/i&gt;" in message.text


def test_an_unbounded_detail_still_fits_the_telegram_budget() -> None:
    """The application layer bounds detail to 240 characters, and this bounds it again — the
    renderer is not entitled to assume the only caller it has today."""
    message = render_activity(
        _group(_activity(ActivityKind.COMPLETED, detail="x" * 20_000)),
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
        _group(_activity(ActivityKind.NEEDS_ANSWER, confidence=ActivityConfidence.REPORTED)),
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
        _group(_activity(ActivityKind.QUIET, confidence=ActivityConfidence.INFERRED)),
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
        _group(_activity(ActivityKind.QUIET, confidence=ActivityConfidence.INFERRED)),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "14:05 UTC" in message.text


def test_quiet_renders_the_same_moment_whatever_offset_it_arrived_in() -> None:
    """An observation is an instant; two spellings of one instant must not read as two."""
    elsewhere = OBSERVED.astimezone(timezone(timedelta(hours=5, minutes=30)))
    message = render_activity(
        _group(_activity(
            ActivityKind.QUIET,
            confidence=ActivityConfidence.INFERRED,
            observed_at=elsewhere,
        )),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "14:05 UTC" in message.text


def test_quiet_never_renders_agent_text_even_if_a_caller_supplies_it() -> None:
    """Nothing said this. A quiet report that carried a parting sentence would present the
    last thing on the screen as a statement the agent chose to make."""
    message = render_activity(
        _group(_activity(
            ActivityKind.QUIET,
            detail="I have completed the migration.",
            confidence=ActivityConfidence.INFERRED,
        )),
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
        _group(_activity(ActivityKind.QUIET, confidence=ActivityConfidence.INFERRED)),
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
                _group(_activity(ActivityKind.COMPLETED)), display=DISPLAY, open_session=rejected
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
        _group(_activity(ActivityKind.LIMIT_REACHED)), display=DISPLAY, open_session=OPEN
    )
    output = render_activity(
        _group(_activity(ActivityKind.OUTPUT_LIMIT)), display=DISPLAY, open_session=OPEN
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
        _group(_activity(ActivityKind.COMPLETED, detail="done \ud800 here")),
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


# Rendering a group -- one message per session, however much it has to say -----------------


def test_a_session_with_several_things_to_say_gets_one_message_saying_all_of_them() -> None:
    """The whole point of grouping, at the renderer.

    Three observations, one message, one name, one button. Asserted on the *count* of lines as
    well as their content, because a renderer that concatenated the three into a paragraph
    would satisfy a substring check and be unreadable on a phone.
    """
    message = render_activity(
        _group(
            _activity(ActivityKind.COMPLETED, detail="Ran the suite."),
            _activity(ActivityKind.NEEDS_ANSWER, detail="Overwrite config.toml?"),
            _activity(ActivityKind.LIMIT_REACHED),
        ),
        display=DISPLAY,
        open_session=OPEN,
    )

    body = message.text.split("\n")
    assert body[0] == f"<b>{DISPLAY}</b>"
    assert len(body) == 4, "one header line and one line per observation"
    assert "finished its work" in body[1] and "Ran the suite." in body[1]
    assert "waiting for an answer" in body[2] and "Overwrite config.toml?" in body[2]
    assert "usage limit" in body[3]
    assert len(message.keyboard[0]) == 1


def test_a_lone_observation_still_reads_exactly_as_it_always_has() -> None:
    """The common case must not pay for the rare one.

    Almost every notification carries one observation, and a bullet in front of a single
    sentence is clutter the owner did not have before. The grouped shape appears only when
    there is something to group.
    """
    message = render_activity(
        _group(_activity(ActivityKind.COMPLETED, detail="Ran the suite.")),
        display=DISPLAY,
        open_session=OPEN,
    )

    assert message.text == f"<b>{DISPLAY}</b>\nThe agent has finished its work.\nRan the suite."


def test_a_session_that_said_more_than_a_message_can_hold_says_how_much_more() -> None:
    """A cap the owner is told about, rather than a silent truncation.

    Seven observations in one pass is already pathological -- the rate limit collapses a
    burst, and the duplicate collapse folds repeats -- so the cap is a backstop. What it may
    not do is drop four observations and look like a complete account of the session.
    """
    message = render_activity(
        _group(
            *(
                _activity(ActivityKind.COMPLETED, detail=f"Step {index}.")
                for index in range(7)
            )
        ),
        display=DISPLAY,
        open_session=OPEN,
    )

    body = message.text.split("\n")
    assert len(body) == 7, "a header, five observations, and the count of what is missing"
    assert "2 earlier" in body[-1]
    assert "Step 6." in message.text, "the newest observation is spelled out, not counted"
    assert "Step 0." not in message.text, "the stalest is what the counter stands for"


def test_one_hedge_covers_a_group_however_many_guesses_are_in_it() -> None:
    """The hedge is a property of the message, not a refrain.

    Repeated per line it would read as emphasis -- as though the service were unusually
    unsure this time -- when it is saying the same structural thing about the same kind.
    """
    message = render_activity(
        _group(
            _activity(ActivityKind.QUIET, confidence=ActivityConfidence.INFERRED),
            _activity(
                ActivityKind.QUIET,
                confidence=ActivityConfidence.INFERRED,
                observed_at=datetime(2026, 8, 11, 15, 30, tzinfo=UTC),
            ),
        ),
        display=DISPLAY,
        open_session=OPEN,
    )

    assert message.text.count("This is a guess") == 1


def test_a_reported_group_carries_no_hedge_at_all() -> None:
    """The other direction, because a hedge appended unconditionally would be worse than none:
    it would teach the owner to discount the reports that are facts."""
    message = render_activity(
        _group(
            _activity(ActivityKind.COMPLETED, detail="Ran the suite."),
            _activity(ActivityKind.NEEDS_ANSWER, detail="Which file?"),
        ),
        display=DISPLAY,
        open_session=OPEN,
    )

    assert "This is a guess" not in message.text


def test_a_grouped_quiet_report_still_carries_no_agent_text() -> None:
    """`QUIET` drops detail regardless of what a caller supplies, and grouping must not be the
    seam where that rule is lost -- the last line of an idle screen rendered under a session's
    name reads exactly like a parting statement."""
    message = render_activity(
        _group(
            _activity(
                ActivityKind.QUIET,
                detail="sk-not-a-real-key-000",
                confidence=ActivityConfidence.INFERRED,
            ),
            _activity(ActivityKind.QUIET, detail="and a transcript", observed_at=OBSERVED),
        ),
        display=DISPLAY,
        open_session=OPEN,
    )

    assert "sk-not-a-real-key-000" not in message.text
    assert "transcript" not in message.text


def test_a_group_of_pathological_details_still_fits_the_telegram_budget() -> None:
    """Five bounded details are not a bounded message.

    The application layer caps each detail at 240 characters, and HTML-escaping can expand a
    character fivefold (`&` becomes `&amp;`), so five details inside one message can reach
    six thousand UTF-16 units against a ceiling of 4096. One observation could never do this,
    which is why the single-observation bound this replaced was sufficient and is not any
    more. Telegram rejects the send outright, so the failure is a notification that never
    arrives rather than one that arrives ugly.
    """
    message = render_activity(
        _group(
            *(
                _activity(
                    ActivityKind.COMPLETED,
                    detail="&" * 240,
                    observed_at=datetime(2026, 8, 11, 14, index, tzinfo=UTC),
                )
                for index in range(5)
            )
        ),
        display="z" * 400,
        open_session=OPEN,
    )

    assert _utf16_units(message.text) <= MAX_TELEGRAM_TEXT_UNITS


def test_a_pathological_name_truncates_itself_rather_than_the_agents_words() -> None:
    """The bounding order, at group scale. A display identity carries an owner-supplied label,
    so it is the attacker-controlled half; the observations are what the message exists to
    deliver. A name fitted first would push them out."""
    message = render_activity(
        _group(
            _activity(ActivityKind.COMPLETED, detail="Ran the suite."),
            _activity(ActivityKind.NEEDS_ANSWER, detail="Which file?"),
        ),
        display="y" * 20_000,
        open_session=OPEN,
    )

    assert _utf16_units(message.text) <= MAX_TELEGRAM_TEXT_UNITS
    assert "Ran the suite." in message.text
    assert "Which file?" in message.text


# Delivering groups -- one message per session per pass ------------------------------------


def _for(session_id: str, kind: ActivityKind, detail: str | None, moment: datetime):
    return AgentActivity(
        session_id=session_id, kind=kind, detail=detail, observed_at=moment, confidence=REPORTED
    )


async def test_a_pass_sends_one_message_per_session_however_much_each_has_to_say() -> None:
    """The cap counts messages now, which is what it was always trying to bound.

    Twenty sessions with two observations each used to be forty sends against a per-chat rate
    limit; it is now twenty, spread across two passes because ten is the per-pass ceiling.
    Nothing is dropped -- the remainder waits.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    burst = [
        activity
        for index in range(20)
        for activity in (
            _for(f"session-{index}", ActivityKind.COMPLETED, "Ran it.", clock.moment),
            _for(f"session-{index}", ActivityKind.NEEDS_ANSWER, "Which file?", clock.moment),
        )
    ]

    assert await notifier.deliver(burst) == 10
    assert len(view.sent) == 10, "ten sessions, ten messages, not twenty sends"
    assert notifier.pending_count() == 20, "the other ten sessions' observations wait"

    assert await notifier.deliver([]) == 10
    assert notifier.pending_count() == 0
    assert len(view.sent) == 20
    for message in view.sent:
        assert str(message["text"]).count("•") == 2, "both observations rode in one message"


async def test_a_refused_group_comes_back_whole_and_regroups_with_what_arrives_next() -> None:
    """The queue is the only copy, so a refused group may not be dropped or split.

    `drain_activity` deletes a record before returning it (DEC-013 cost 3), and DEC-026 keeps
    this queue in memory with nothing behind it, so an activity that reaches here and is
    neither sent nor held is gone. The regrouping half matters just as much: a group held from
    an earlier pass must merge with news that arrived since, or the owner gets two messages
    about one session and the grouping has bought nothing.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    class _Refusing:
        chat_id = 11

        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.refuse = True

        async def send_apart(self, _bot: object, arguments: dict[str, object]) -> int:
            if self.refuse:
                raise RuntimeError("Telegram is not answering")
            self.sent.append(arguments)
            return 901

    refusing = _Refusing()
    notifier._view = refusing

    first = _for(SESSION_A, ActivityKind.COMPLETED, "Ran it.", clock.moment)
    assert await notifier.deliver([first]) == 0
    assert notifier.pending_count() == 1, "a refused group is held, not lost"

    refusing.refuse = False
    clock.advance(30)
    later = _for(SESSION_A, ActivityKind.NEEDS_ANSWER, "Which file?", clock.moment)

    assert await notifier.deliver([later]) == 1
    assert len(refusing.sent) == 1, "one message, not one for the held half and one for the new"
    text = str(refusing.sent[0]["text"])
    assert "Ran it." in text and "Which file?" in text


async def test_a_group_that_the_rate_limit_empties_sends_nothing_at_all() -> None:
    """An emptied group is finished business, not a failed send.

    Every observation in it has already been reported inside its window, so there is nothing
    to say -- and holding it for retry would mean re-deciding the same suppression every pass
    for as long as the window lasts.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    first = _for(SESSION_A, ActivityKind.COMPLETED, "Ran it.", clock.moment)

    assert await notifier.deliver([first]) == 1

    clock.advance(10)
    again = _for(SESSION_A, ActivityKind.COMPLETED, "Ran it again.", clock.moment)
    assert await notifier.deliver([again]) == 0
    assert len(view.sent) == 1, "the second is inside the window and is not a second message"
    assert notifier.pending_count() == 0, "suppressed is settled, not held"


async def test_two_kinds_in_one_message_do_not_reset_each_other_s_backoff() -> None:
    """The cross-kind reset was written when a message carried exactly one kind.

    Its rule is sound: a *different* kind means something changed, so the session's other
    repeat counts are a claim that nothing has, and they start over. Applied to two kinds
    riding in one message it turns on itself -- recording the second zeroes the first, which
    was recorded a line earlier and is not evidence that anything changed. A standing
    condition would then reset its own backoff on every pass that carried a companion, and the
    doubling that exists to stop a three-in-the-morning message every two minutes would never
    advance past its first step.

    What may still reset is a kind that was *not* in this message: that is the original rule,
    and it is left alone.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    # A standing condition, repeated until its window has doubled twice. Each advance clears
    # the *current* window without reaching the retention horizon, which is that window times
    # `_RETENTION_WINDOWS` -- overshoot it and the entry is forgotten, so the next send reads
    # as a first sighting and the count never climbs.
    await notifier.deliver([_for(SESSION_A, ActivityKind.NEEDS_ANSWER, None, clock.moment)])
    for seconds in (121, 241):
        clock.advance(seconds)
        await notifier.deliver([_for(SESSION_A, ActivityKind.NEEDS_ANSWER, None, clock.moment)])
    waiting = notifier._last_sent[(SESSION_A, ActivityKind.NEEDS_ANSWER)]
    assert waiting.repeats == 2, "the standing condition must have backed off to begin with"

    clock.advance(481)

    # Now it repeats *alongside* a second kind, in one message.
    await notifier.deliver(
        [
            _for(SESSION_A, ActivityKind.NEEDS_ANSWER, None, clock.moment),
            _for(SESSION_A, ActivityKind.COMPLETED, "Ran it.", clock.moment),
        ]
    )

    assert notifier._last_sent[(SESSION_A, ActivityKind.NEEDS_ANSWER)].repeats > waiting.repeats, (
        "its own companion must not have reset it"
    )


async def test_a_kind_that_was_not_in_the_message_still_starts_over() -> None:
    """The half of the reset that is still right, pinned so the fix above cannot delete it."""
    clock = _Clock()
    notifier, view = _notifier(clock)

    await notifier.deliver([_for(SESSION_A, ActivityKind.COMPLETED, None, clock.moment)])
    for seconds in (121, 241):
        clock.advance(seconds)
        await notifier.deliver([_for(SESSION_A, ActivityKind.COMPLETED, None, clock.moment)])
    assert notifier._last_sent[(SESSION_A, ActivityKind.COMPLETED)].repeats == 2

    await notifier.deliver([_for(SESSION_A, ActivityKind.NEEDS_ANSWER, None, clock.moment)])

    assert notifier._last_sent[(SESSION_A, ActivityKind.COMPLETED)].repeats == 0, (
        "a different kind means something changed, so the untouched kind starts over"
    )


async def test_a_suppressed_kind_does_not_have_its_own_backoff_reset_by_its_suppression() -> None:
    """The storm the gate's evaluator measured: 75 to 255 messages where the taper intends 12.

    The mechanism was the notifier reading its own suppression as evidence against itself. A
    standing `needs_answer` backed off to sixty-four minutes is absent from sixty-three of every
    sixty-four minutes' messages; the send filtered it out for being inside its window, and
    `_record_sent` was then told only about the kinds that survived that filter, so it saw the
    held kind as "not in this message" and read that as the session having reported something
    different. Its backoff went to zero and it fired again on the next thirty-second pass.

    `Stop` fires per turn, so a companion kind arriving periodically is the ordinary case for an
    agent working through a long instruction -- not a contrived one.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    await notifier.deliver([_for(SESSION_A, ActivityKind.NEEDS_ANSWER, None, clock.moment)])
    for seconds in (121, 241):
        clock.advance(seconds)
        await notifier.deliver([_for(SESSION_A, ActivityKind.NEEDS_ANSWER, None, clock.moment)])
    backed_off = notifier._last_sent[(SESSION_A, ActivityKind.NEEDS_ANSWER)].repeats
    assert backed_off == 2

    # A companion kind arrives while the standing one is still inside its window.
    clock.advance(60)
    await notifier.deliver(
        [
            _for(SESSION_A, ActivityKind.NEEDS_ANSWER, None, clock.moment),
            _for(SESSION_A, ActivityKind.COMPLETED, "Ran the linter.", clock.moment),
        ]
    )

    assert notifier._last_sent[(SESSION_A, ActivityKind.NEEDS_ANSWER)].repeats > backed_off, (
        "the held kind's own suppression must not read as a change"
    )
    assert "waiting for an answer" in str(view.sent[-1]["text"]), (
        "and since a message was going out anyway, it rides along rather than being deleted"
    )


async def test_an_observation_is_never_deleted_from_a_message_that_is_going_out() -> None:
    """Anything still queued has by construction never been sent, so dropping it loses it.

    The window exists to stop *messages*, and a second line inside one the owner is already
    receiving costs them nothing. Filtering the group by window meant a message went out with
    one line used and four spare while something the agent had said was discarded.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    await notifier.deliver([_for(SESSION_A, ActivityKind.COMPLETED, "wrote the parser", 
                                 clock.moment)])
    clock.advance(10)
    await notifier.deliver(
        [
            _for(SESSION_A, ActivityKind.COMPLETED, "and then ran the suite", clock.moment),
            _for(SESSION_A, ActivityKind.NEEDS_ANSWER, "May I push?", clock.moment),
        ]
    )

    latest = str(view.sent[-1]["text"])
    assert "May I push?" in latest
    assert "and then ran the suite" in latest, "a distinct, never-sent line was deleted"
    assert notifier.pending_count() == 0


async def test_a_full_queue_costs_the_session_that_filled_it_not_the_quiet_ones() -> None:
    """Retention was global while delivery was per session, so one session could evict all news.

    `observe_quiet` reports once per spell and re-arms only when the pane changes, and the drain
    deletes a record before returning it, so a quiet session's evicted report is gone for good --
    there is no second chance anywhere in the system.
    """
    clock = _Clock()
    notifier, _ = _notifier(clock)
    notifier._bot = None  # hold everything: no delivery, so the cap is what is under test

    for index in range(5):
        await notifier.deliver([_for(f"quiet-{index}", ActivityKind.QUIET, None, clock.moment)])
    for index in range(400):
        await notifier.deliver(
            [_for("loud", ActivityKind.COMPLETED, f"step {index}", clock.moment)]
        )

    held = {activity.session_id for activity in notifier._pending}
    assert {f"quiet-{index}" for index in range(5)} <= held, (
        "the quiet sessions' only reports were evicted by a louder neighbour"
    )
