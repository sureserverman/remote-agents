from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest
from telegram.error import TelegramError

from remote_agents.adapters.telegram import notifications
from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.notifications import (
    OPEN_SESSION_LABEL,
    ActivityNotifier,
    render_activity,
)
from remote_agents.adapters.telegram.presenters import MAX_TELEGRAM_TEXT_UNITS
from remote_agents.application.notification_policy import (
    REFUSALS_BEFORE_ABANDONING,
    SessionGroup,
)
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
            _group(
                _activity(
                    other,
                    confidence=(
                        ActivityConfidence.INFERRED
                        if other is ActivityKind.QUIET
                        else ActivityConfidence.REPORTED
                    ),
                )
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
        _group(
            _activity(
                ActivityKind.QUIET,
                confidence=ActivityConfidence.INFERRED,
                observed_at=elsewhere,
            )
        ),
        display=DISPLAY,
        open_session=OPEN,
    )
    assert "14:05 UTC" in message.text


def test_quiet_never_renders_agent_text_even_if_a_caller_supplies_it() -> None:
    """Nothing said this. A quiet report that carried a parting sentence would present the
    last thing on the screen as a statement the agent chose to make."""
    message = render_activity(
        _group(
            _activity(
                ActivityKind.QUIET,
                detail="I have completed the migration.",
                confidence=ActivityConfidence.INFERRED,
            )
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
    """The `LiveView` surface a notifier actually uses: send, amend and discard, one chat."""

    chat_id = 11

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.ids: list[int] = []
        self.deleted: list[int] = []
        self.amended: list[tuple[int, dict[str, object]]] = []
        #: Every text this chat was made to show, in the order it was written, by whichever
        #: route wrote it. What the owner is looking at is the last of these.
        self.written: list[dict[str, object]] = []
        self.refuse_delete = False
        self.refuse_amend = False
        self._next_id = 900

    async def send_apart(self, _bot: object, arguments: dict[str, object]) -> int:
        self.sent.append(arguments)
        self.written.append(arguments)
        self._next_id += 1
        self.ids.append(self._next_id)
        return self._next_id

    async def amend_apart(
        self, _bot: object, message_id: int, arguments: dict[str, object]
    ) -> bool:
        if self.refuse_amend:
            return False
        self.amended.append((message_id, arguments))
        self.written.append(arguments)
        return True

    async def discard(self, _bot: object, message_id: int) -> bool:
        if self.refuse_delete:
            return False
        self.deleted.append(message_id)
        return True


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


def _notifier(clock: _Clock, *, callbacks=None):
    view = _RecordingView()

    async def display(_session_id: str) -> str:
        return DISPLAY

    notifier = ActivityNotifier(
        view=view,
        callbacks=callbacks if callbacks is not None else CallbackStateStore(),
        owner_user_id=7,
        display=display,
        now=clock,
    )
    notifier.attach(_SilentBot())
    return notifier, view


def _showing(view: _RecordingView) -> str:
    """What the session's message says right now.

    The newest text written to the chat by either route, since a repeat is amended into the
    message the owner already has rather than sent as a new one -- so reading `sent` alone
    would report the text as it stood before the last few reports.
    """
    return str(view.written[-1]["text"])


def _messages(view: _RecordingView) -> int:
    """How many of this session's messages are *in the chat*, not how many were ever sent.

    The distinction is the whole delivery shape. An update the owner has not been alerted to
    sends a message and deletes the one it replaces, so the sends climb while the chat holds
    exactly one -- and it is the second number the owner experiences. A repeat does not even
    do that: it is amended into the message they already have and `sent` does not move.
    """
    return len(view.sent) - len(view.deleted)


async def test_a_later_report_of_the_same_kind_never_reaches_the_owner_s_phone_again() -> None:
    """What a second `completed` owes the owner is nothing at all: they have already been told.

    Two shapes were tried before this one and each fixed half of it. The rate limit suppressed
    the repeat and then, once the window passed, sent the later report as a message of its own
    -- three messages about one session, an hour of scrolling between the first and the last.
    Replacing the session's message fixed the chat and not the phone: every replacement is a
    `sendMessage`, so the owner was still buzzed per turn and still watched one message jump
    to the bottom over and over.

    The rule the owner asked for is the one pinned here. `completed` says the session stopped
    and wants them, it says that exactly once, and a fresher copy of it is written into the
    message they have -- silently, where it is.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    first = _activity(ActivityKind.COMPLETED, detail="wrote the parser")

    assert await notifier.deliver([first]) == 1
    assert await notifier.deliver([first]) == 0, "the same sentence twice is one sentence"

    clock.advance(121)
    later = _activity(
        ActivityKind.COMPLETED,
        detail="ran the suite",
        observed_at=OBSERVED + timedelta(seconds=121),
    )
    assert await notifier.deliver([later]) == 1

    assert _messages(view) == 1, "the session still occupies one message"
    assert len(view.sent) == 1, "and nothing arrived on the owner's phone a second time"
    assert [message_id for message_id, _ in view.amended] == [view.ids[0]], (
        "the amendment must land on the message the owner already has"
    )
    assert view.amended[-1][1]["reply_markup"] is not None, (
        "an edit that carries no markup takes the Open session button away"
    )
    showing = _showing(view)
    assert "ran the suite" in showing
    assert "wrote the parser" not in showing, "just the last of them, not a pile of them"


async def test_news_of_a_kind_the_owner_has_not_been_told_arrives_as_a_message() -> None:
    """The silence is for repeats. A sentence the message does not carry is worth a buzz.

    `needs_answer` behind a `completed` is the case the whole distinction exists for: the
    session has gone from stopped to blocked on the owner, which is the one thing this service
    knows that they cannot see. Amending it in would leave that sitting silently in a message
    they have already read and scrolled past.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    session = _activity(ActivityKind.COMPLETED).session_id

    first = _for(session, ActivityKind.COMPLETED, "done", clock.moment)
    assert await notifier.deliver([first]) == 1
    clock.advance(121)
    assert (
        await notifier.deliver([_for(session, ActivityKind.NEEDS_ANSWER, "May I?", clock.moment)])
        == 1
    )

    assert len(view.sent) == 2, "a kind the owner has not seen has to arrive, not be amended"
    assert _messages(view) == 1, "and it still costs the chat only one message"
    showing = _showing(view)
    assert "May I?" in showing
    assert "done" in showing, "the message it replaced carried news it must not drop"


async def test_a_pass_that_only_amends_does_not_re_send_the_owner_s_menu() -> None:
    """`move_to_bottom` deletes and re-sends the live view, which is itself a message arriving.

    So a pass that put nothing new below the menu must not move it. Left ungated, the silence
    would have been undone one layer out: the notification stays quiet and the menu buzzes in
    its place, once per pass, for as long as the session keeps reporting.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    moved: list[object] = []

    async def move_to_bottom(bot: object) -> int | None:
        moved.append(bot)
        return None

    view.move_to_bottom = move_to_bottom
    session = _activity(ActivityKind.COMPLETED).session_id

    await notifier.deliver([_for(session, ActivityKind.COMPLETED, "one", clock.moment)])
    assert len(moved) == 1, "a first notification does arrive below the menu"

    clock.advance(121)
    await notifier.deliver([_for(session, ActivityKind.COMPLETED, "two", clock.moment)])

    assert len(moved) == 1, "an amendment moved the menu, and moving it is a message"


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


async def test_a_message_the_owner_has_opened_is_replaced_rather_than_edited() -> None:
    """Pressing Open session consumes the message, so the next news needs a new one.

    `service` deletes it and tells the notifier; without that the next report would be edited
    into a message that is no longer in the chat, and the owner would hear nothing at all
    until Telegram got round to refusing the edit.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    session = _activity(ActivityKind.COMPLETED).session_id

    assert await notifier.deliver([_for(session, ActivityKind.COMPLETED, "one", clock.moment)]) == 1
    notifier.forget(session)

    clock.advance(121)
    assert await notifier.deliver([_for(session, ActivityKind.COMPLETED, "two", clock.moment)]) == 1

    assert _messages(view) == 2, "a consumed message cannot be amended"
    assert "two" in str(view.sent[-1]["text"])
    assert "one" not in str(view.sent[-1]["text"]), "the new message starts from now"


async def test_the_replacement_carries_the_button_rather_than_minting_a_second() -> None:
    """The token moves onto the new message, so a session reporting all day costs one row.

    `rebind` is the same answer `LiveView.move_to_bottom` gives when it re-sends the menu, and
    for the same two reasons: the store is bounded by size and evicts oldest-first, and a
    keyboard that stopped resolving across the move would be a dead button.

    Driven with a *second kind*, because that is what a replacement is now: a repeat is
    amended into the message it belongs to and never moves anything to rebind.
    """
    clock = _Clock()
    store = CallbackStateStore()
    notifier, view = _notifier(clock, callbacks=store)
    session = _activity(ActivityKind.COMPLETED).session_id

    await notifier.deliver([_for(session, ActivityKind.COMPLETED, "one", clock.moment)])
    after_first = store.active_count()
    clock.advance(121)
    await notifier.deliver([_for(session, ActivityKind.NEEDS_ANSWER, "two", clock.moment)])

    assert store.active_count() == after_first, "a replacement minted a second token"
    store.bind_pending(11, view.ids[-1])
    token = next(
        button.callback_data
        for row in view.sent[-1]["reply_markup"].inline_keyboard
        for button in row
    )
    resolved = store.resolve(token, owner_id=7, chat_id=11, message_id=view.ids[-1])
    assert resolved is not None, "the button on the newest message does not resolve"
    assert resolved.entity_id == session


async def test_a_refused_send_leaves_the_message_the_owner_already_had() -> None:
    """A 429 is Telegram asking for a moment, not a reason to lose what the agent said.

    Send-then-delete is the order that makes this safe: the replacement never landed, so the
    message it was replacing is still in the chat, and the news is still owed.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    session = _activity(ActivityKind.COMPLETED).session_id

    await notifier.deliver([_for(session, ActivityKind.COMPLETED, "one", clock.moment)])

    async def refuse(_bot: object, _arguments: dict[str, object]) -> int:
        raise TelegramError("Flood control exceeded")

    view.send_apart = refuse
    clock.advance(121)
    # A second kind, because only news the owner has not been alerted to is *sent* at all.
    assert (
        await notifier.deliver([_for(session, ActivityKind.NEEDS_ANSWER, "two", clock.moment)]) == 0
    )

    assert _messages(view) == 1, "a failed send took away the message the owner had"
    assert view.deleted == [], "nothing may be deleted before its replacement lands"
    assert notifier.pending_count() == 1, "and the news is owed, not spent"


async def test_a_standing_condition_never_becomes_a_second_message() -> None:
    """`needs_answer` repeats for as long as the owner does not answer — all night.

    The backoff was the old answer: double the window so the copies get rarer, twelve
    messages instead of two hundred. One message per session is the stronger answer, because
    the number it bounds is zero. A question still waiting at three in the morning is the
    same question, and it is already on the owner's screen.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    waiting = _activity(ActivityKind.NEEDS_ANSWER, detail="May I force-push?")

    assert await notifier.deliver([waiting]) == 1
    for _ in range(20):
        clock.advance(121)
        assert await notifier.deliver([waiting]) == 0, "a repeat is not news"

    assert _messages(view) == 1
    assert "May I force-push?" in _showing(view)


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
        _group(*(_activity(ActivityKind.COMPLETED, detail=f"Step {index}.") for index in range(7))),
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


async def test_a_kind_the_window_is_holding_is_never_deleted_from_a_message_going_out() -> None:
    """Anything still queued has by construction never been sent, so dropping it loses it.

    The window exists to stop *messages*, and a second line inside one the owner is already
    receiving costs them nothing. Filtering the group by window meant a message went out with
    one line used and four spare while something the agent had said was discarded.

    `completed` here is inside its window and `needs_answer` is not, so the message goes out
    for the second and must carry the first. What it carries of `completed` is the newer
    wording, which is the collapse working rather than a line going missing -- the older words
    are superseded, not withheld.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    await notifier.deliver(
        [_for(SESSION_A, ActivityKind.COMPLETED, "wrote the parser", clock.moment)]
    )
    clock.advance(10)
    await notifier.deliver(
        [
            _for(SESSION_A, ActivityKind.COMPLETED, "and then ran the suite", clock.moment),
            _for(SESSION_A, ActivityKind.NEEDS_ANSWER, "May I push?", clock.moment),
        ]
    )

    latest = _showing(view)
    assert "May I push?" in latest
    assert "and then ran the suite" in latest, "a suppressed kind was deleted rather than carried"
    assert _messages(view) == 1
    assert notifier.pending_count() == 0


async def test_a_full_queue_costs_the_session_that_filled_it_not_the_quiet_ones() -> None:
    """Retention was global while delivery was per session, so one session could evict all news.

    The drain deletes a record before returning it, so a quiet session's evicted report is gone
    for good -- there is no second chance anywhere in the system.
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


async def test_an_observation_the_message_could_not_hold_is_owed_not_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line cap must not become a second way to lose the highest-value signal.

    Both gate readers found this independently. `activity_text` spells out the newest five
    observations and folds the rest into "and N earlier"; `_send` was stamping the rate limit
    for *every* kind in the group. So a `needs_answer` sitting behind five newer `completed`
    reports was never rendered, recorded as though it had been told to the owner, and then
    dropped -- a successfully sent group is not re-queued. The agent is waiting, nobody is told,
    and the stamp suppresses the next report of it too.

    The cap is driven down to two for this, because collapsing a session's news on the kind
    put it out of ordinary reach: there are fewer kinds than lines, so a real group no longer
    overflows. It is kept, and kept tested, as the backstop for a kind being added -- the two
    defects above are properties of the fold, not of the number, and they would come back with
    it.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    monkeypatch.setattr(notifications, "_MAXIMUM_LINES_PER_MESSAGE", 2)
    waiting = _for(SESSION_A, ActivityKind.NEEDS_ANSWER, "Which file?", clock.moment)
    clock.advance(1)
    newer = [
        _for(SESSION_A, ActivityKind.COMPLETED, "turn 4", clock.moment),
        _for(
            SESSION_A,
            ActivityKind.LIMIT_REACHED,
            "out of budget",
            clock.moment + timedelta(seconds=1),
        ),
    ]

    assert await notifier.deliver([waiting, *newer]) == 1

    text = str(view.sent[-1]["text"])
    assert "and 1 earlier." in text, "the oldest is the one folded away"
    assert "Which file?" not in text
    assert notifier.pending_count() == 1, "it is owed, not spent"

    # The next pass says it, rather than it being lost with the group that could not carry it.
    # It has to claim a slot from a line the message already showed: there is no second
    # message to escape into, so an owed observation that merely re-merged would lose the
    # same contest every pass, forever.
    clock.advance(1)
    assert await notifier.deliver([]) == 1
    assert "Which file?" in _showing(view)
    assert _messages(view) == 1


async def test_a_permanently_refused_group_is_dropped_and_stops_blocking_the_chat() -> None:
    """BL-003, closed. Three strikes and the poison goes (DEC-049).

    `deliver` stops the pass on a refusal and holds the group at the head of the queue, which
    is right for a 429 or an outage. For a refusal that will never succeed it meant the same
    group was retried and refused every pass -- and because the refusal stops the pass, **no
    session in the chat was ever notified again**. One poisoned group was a chat-wide outage
    that nothing reported.

    The second assertion is the one that matters. Dropping the poison is only half the fix:
    the pass carries on past a refusal now, so the session waiting behind it is served in the
    same pass rather than after the poison is finally abandoned.

    **Both sessions report on every pass, and that is load-bearing rather than incidental.** A
    strike is only recorded when something else got through in the same pass (DEC-049), which
    is what stops an outage being read as poison -- so a poisoned session that is *alone* in
    the queue accumulates no strikes and is held indefinitely. That is the accepted cost, and
    it is coherent: a group blocking nobody is not the failure BL-003 named.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    honest_send = view.send_apart

    async def refuse_the_poisoned_one(bot: object, arguments: dict[str, object]) -> int:
        if "poisoned" in str(arguments["text"]):
            raise TelegramError("400: this message can never be sent")
        return await honest_send(bot, arguments)

    view.send_apart = refuse_the_poisoned_one  # type: ignore[method-assign]

    for attempt in range(REFUSALS_BEFORE_ABANDONING):
        clock.advance(60)
        await notifier.deliver(
            [
                _for(SESSION_A, ActivityKind.COMPLETED, "poisoned", clock.moment),
                _for(
                    SESSION_B, ActivityKind.NEEDS_ANSWER, f"am I ever told? {attempt}", clock.moment
                ),
            ]
        )

    assert "am I ever told?" in _showing(view), (
        "the second session is still waiting behind the poisoned group; dropping the poison "
        "without letting the pass continue only shortens the outage, it does not end it"
    )
    assert not any(activity.session_id == SESSION_A for activity in notifier._pending), (
        "the poisoned group is still queued after its third refusal"
    )


async def test_an_outage_is_not_read_as_poison_and_costs_the_owner_nothing() -> None:
    """The reason a strike is conditional (DEC-049), and the case a bare count gets wrong.

    A refusal on its own says nothing about why: a 400 on a malformed message and a network
    that is down are the same exception here. A plain three-strike rule cannot tell them
    apart, so a sustained outage would quietly abandon a session's news every few passes --
    ten sessions over half an hour at a sixty-second poll, with only journal lines to show for
    it. That is a worse failure than the one BL-003 named, because it destroys news that would
    have gone out fine on the next attempt.

    So a strike is only recorded when *something else got through in the same pass*. Here
    nothing does, for twice the limit, and every observation is still owed at the end.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    async def telegram_is_down(_bot: object, _arguments: dict[str, object]) -> int:
        raise TelegramError("network is unreachable")

    view.send_apart = telegram_is_down  # type: ignore[method-assign]

    for attempt in range(REFUSALS_BEFORE_ABANDONING * 2):
        clock.advance(60)
        await notifier.deliver(
            [
                _for(SESSION_A, ActivityKind.COMPLETED, f"a {attempt}", clock.moment),
                _for(SESSION_B, ActivityKind.NEEDS_ANSWER, f"b {attempt}", clock.moment),
            ]
        )

    assert notifier._refusals == {}, (
        "an outage was scored as poison; a bare count cannot tell them apart, which is why "
        "the strike is conditional"
    )
    queued = {activity.session_id for activity in notifier._pending}
    assert queued == {SESSION_A, SESSION_B}, (
        f"the outage cost the owner news that would have gone out on recovery: {queued}"
    )


async def test_a_session_that_recovers_never_reaches_the_limit() -> None:
    """A session that fails twice, recovers, then fails twice more keeps its news.

    A transient refusal is the case the retry exists for. Five refusals across this session's
    life, never three in a row, and its observations are still owed at the end.

    **What this does not prove, stated because the mutation was run.** It does not isolate
    *which* mechanism clears the streak. Removing `delivered_before` entirely leaves this test
    green, because `forget_absent` already drops the count for a session that has left the
    queue -- and a session that delivered has left the queue on every path reachable here. The
    arm only `delivered_before` covers is a session that delivers and *stays* queued, which
    needs a message too small to hold its lines, and there are fewer kinds than lines by
    design. So that arm is carried on argument rather than evidence, and the source says so at
    the call.

    **Two earlier versions of this test proved less than they claimed**, which is why the
    caveat above is here rather than absent. The first used one session, and a lone session
    accrues no strikes at all -- a strike needs another delivery in the same pass (DEC-049) --
    so it passed with the clearing removed. The second refused only `send_apart`, so once the
    session recovered its later reports became amendments and stopped being refused at all.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)
    honest_send = view.send_apart
    honest_amend = view.amend_apart
    refuse_session_a = True

    async def refuse_a_when_told(bot: object, arguments: dict[str, object]) -> int:
        if refuse_session_a and "flaky" in str(arguments["text"]):
            raise TelegramError("429: slow down")
        return await honest_send(bot, arguments)

    async def refuse_amendments_too(
        bot: object, message_id: int, arguments: dict[str, object]
    ) -> bool:
        # Both routes, because once a session has a standing message its next report is an
        # amendment and never reaches `send_apart` at all. A fake that refuses only sends
        # stops refusing the moment the session recovers, which is exactly the shape this
        # test is trying to drive through.
        if refuse_session_a and "flaky" in str(arguments["text"]):
            raise TelegramError("429: slow down")
        return await honest_amend(bot, message_id, arguments)

    view.send_apart = refuse_a_when_told  # type: ignore[method-assign]
    view.amend_apart = refuse_amendments_too  # type: ignore[method-assign]

    async def a_pass(index: int) -> None:
        clock.advance(60)
        await notifier.deliver(
            [
                _for(SESSION_A, ActivityKind.COMPLETED, f"flaky {index}", clock.moment),
                _for(SESSION_B, ActivityKind.COMPLETED, f"steady {index}", clock.moment),
            ]
        )

    await a_pass(0)
    await a_pass(1)
    assert notifier._refusals.get(SESSION_A) == 2, "the premise: two strikes stand"

    refuse_session_a = False
    await a_pass(2)
    assert notifier._refusals.get(SESSION_A) is None, "a delivery did not clear the streak"

    refuse_session_a = True
    await a_pass(3)
    await a_pass(4)

    # Five refusals in this session's life, never three in a row.
    assert notifier._refusals.get(SESSION_A) == 2, (
        "the streak is being counted for the life of the process rather than consecutively"
    )
    assert any(activity.session_id == SESSION_A for activity in notifier._pending), (
        "a session that recovers in between was abandoned; its news would have gone out"
    )


async def test_the_refusal_counts_do_not_grow_for_the_life_of_the_service() -> None:
    """The map this rule needs is the shape of the one DEC-048 deleted, so it is bounded here.

    A session can leave the queue without succeeding *or* being abandoned -- the 200-cap evicts
    it, or `retire_finished` retires it -- and a count nobody will ever clear is exactly the
    unbounded map the taper left behind.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    honest_send = view.send_apart

    async def refuse_only_session_a(bot: object, arguments: dict[str, object]) -> int:
        if "held" in str(arguments["text"]):
            raise TelegramError("400")
        return await honest_send(bot, arguments)

    view.send_apart = refuse_only_session_a  # type: ignore[method-assign]

    clock.advance(60)
    # A delivery beside the refusal, because a strike is only recorded when the service is
    # otherwise working (DEC-049).
    await notifier.deliver(
        [
            _for(SESSION_A, ActivityKind.COMPLETED, "held", clock.moment),
            _for(SESSION_B, ActivityKind.COMPLETED, "went out", clock.moment),
        ]
    )
    assert notifier._refusals, "the premise: a refusal was counted"

    # The session's news leaves the queue by a route that is neither success nor abandonment.
    notifier._pending.clear()
    clock.advance(60)
    await notifier.deliver([])

    assert notifier._refusals == {}, "a count outlived every observation it was about"


# What replaced the suppression window (DEC-048) ----------------------------------------------


async def test_a_second_question_reaches_the_owner_rather_than_being_discarded() -> None:
    """BL-002, closed. The case the taper destroyed, now the case that earns an alert.

    A window keyed on `(session, kind)` used to gate this, and a gated observation was neither
    sent nor held: `deliver` dropped it and the drain had already deleted the record, so the
    owner was never told -- not late, not silently, not at all -- that the question they were
    reading had been superseded. `needs_answer` is the kind where that mattered most, because
    the agent is *blocked* on it.

    `unheard` now compares the question rather than the kind, so a different one is news. The
    message count moving is the assertion that matters: an amendment is silent and would leave
    the owner's phone quiet.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    assert (
        await notifier.deliver(
            [_for(SESSION_A, ActivityKind.NEEDS_ANSWER, "Which file?", clock.moment)]
        )
        == 1
    )
    assert "Which file?" in _showing(view)
    sends_after_the_first = len(view.sent)

    clock.advance(30)
    assert (
        await notifier.deliver(
            [_for(SESSION_A, ActivityKind.NEEDS_ANSWER, "May I force-push to main?", clock.moment)]
        )
        == 1
    ), "the new question was suppressed; this is exactly BL-002"

    assert "May I force-push to main?" in _showing(view)
    # `len(view.sent)` is the only assertion that says "reached the phone". `deliver`'s return
    # counts amendments, `_messages` is `sent - deleted` and a replacement does both, and
    # `_showing` reads the text either way -- three ways to write a green test that would pass
    # with the exception removed. The first draft of this test used two of them.
    assert len(view.sent) > sends_after_the_first, (
        "the new question was amended in silently; the owner's phone never rang, which is the "
        "half of BL-002 that removing the window alone does not fix"
    )
    assert notifier.pending_count() == 0, "delivered, so nothing is owed"


async def test_the_same_question_asked_again_never_reaches_the_phone() -> None:
    """The taper's real job, done by comparing what was said -- and what that now costs.

    An agent repeating one sentence is the burst case the window was built for, and removing
    the window did not reopen it: the repeat carries no kind the standing message does not
    already carry, so `unheard` is empty and it is **amended in place**. `amend_apart` is the
    silent route, so the owner's phone stays quiet however long the agent repeats itself,
    which is the whole of what the taper protected.

    **The cost, stated because it is real (DEC-048 accepted cost 1).** The window used to gate
    amendments too, so a repeating session cost nothing at all; now it costs one `editMessageText`
    per pass. `merged` stamps the newer `observed_at`, so an ordinary repeat is never byte-equal
    to what is standing and does not take the early return -- that only fires for a re-render
    identical down to the timestamp. It is bounded by `_MAXIMUM_SENDS_PER_PASS`, which counts
    amendments, and it buys the owner never losing a question.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    question = _for(SESSION_A, ActivityKind.NEEDS_ANSWER, "Which file?", clock.moment)
    assert await notifier.deliver([question]) == 1
    messages_after_the_first = _messages(view)
    sends_after_the_first = len(view.sent)

    for _ in range(5):
        clock.advance(1)
        await notifier.deliver(
            [_for(SESSION_A, ActivityKind.NEEDS_ANSWER, "Which file?", clock.moment)]
        )

    assert _messages(view) == messages_after_the_first, "a repeat put a second message in the chat"
    assert len(view.sent) == sends_after_the_first, (
        "a repeat sent a message; sending is what arrives on the phone, and an identical "
        "question is not news"
    )
    assert "Which file?" in _showing(view)
    assert notifier.pending_count() == 0, "and it is not owed either -- it is on screen"


async def test_a_fresher_completion_is_still_amended_in_silently() -> None:
    """The exemption is `needs_answer` alone, and this is what it is scoped against.

    `completed` says the session stopped and says it once, however its last reply is worded --
    so a fresher one carrying different text is the same news in newer words and must not
    buzz. Comparing the observations for *every* kind is what would make every repeat an alert
    again, which is the shape the owner asked to be rid of; this is the other side of that line
    and the reason `unheard` is text-aware for one kind rather than for all of them.
    """
    clock = _Clock()
    notifier, view = _notifier(clock)

    assert (
        await notifier.deliver(
            [_for(SESSION_A, ActivityKind.COMPLETED, "wrote a.py", clock.moment)]
        )
        == 1
    )
    sends_after_the_first = len(view.sent)

    clock.advance(30)
    assert (
        await notifier.deliver(
            [_for(SESSION_A, ActivityKind.COMPLETED, "wrote b.py", clock.moment)]
        )
        == 1
    )

    assert "wrote b.py" in _showing(view), "the newer words are written into the message"
    assert len(view.sent) == sends_after_the_first, (
        "a fresher completion reached the phone; it is the same news in newer words, and "
        "alerting for it is the shape DEC-034 removed"
    )


async def test_the_queue_full_warning_names_the_session_that_paid(caplog) -> None:
    """BL-032, closed. The id has to be in the record, not merely in the format string.

    `test_every_operator_facing_sentence_is_the_one_it_has_always_been` pins the sentence, and
    that is the guard that made this a deliberate edit rather than a drive-by reword. But it
    reads the *literal* out of the source and nothing more, so it would pass just as happily
    with the arguments the wrong way round -- "for session 5 (loud-session held)" is a
    different defect wearing the same string. This drives a real eviction and reads the record
    the logger actually emitted.
    """
    clock = _Clock()
    notifier, _view = _notifier(clock)

    loud = [
        _for(SESSION_A, ActivityKind.COMPLETED, f"run {index}", clock.moment)
        for index in range(notifications._MAXIMUM_PENDING + 1)
    ]
    with caplog.at_level(logging.WARNING, logger="remote_agents.adapters.telegram.notifications"):
        # Queued while there is no bot attached, so the pass holds rather than delivers and the
        # cap is genuinely reached.
        notifier._bot = None
        await notifier.deliver(loud)

    warnings = [r for r in caplog.records if "queue is full" in r.getMessage()]
    assert warnings, "the cap was never reached; this test proves nothing"
    message = warnings[-1].getMessage()
    assert f"for session {SESSION_A}" in message, (
        f"the session that paid is not named, or is named in the wrong slot: {message!r}"
    )
    assert "held)" in message and str(notifications._MAXIMUM_PENDING) in message, (
        f"the tally the id sits beside is gone or wrong: {message!r}"
    )
