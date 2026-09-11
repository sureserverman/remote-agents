"""The pass that asks the folder-trust question on its own, and answers itself once settled.

The three properties worth having, and each one is a way the pass could be wrong:
one message per session however many passes run; a state change amends in place rather than
arriving again (DEC-034, DEC-048); and a chat that keeps refusing is given up on rather than
hammered forever (DEC-049).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from telegram.error import BadRequest

from remote_agents.adapters.agents.registry import profile_trust_dialogs
from remote_agents.adapters.telegram.trust_notifications import TrustNotifier
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_SESSION = SessionId.new()


def _record(state: SessionState = SessionState.UNTRUSTED, profile: str = "claude") -> SessionRecord:
    return SessionRecord(
        _SESSION,
        ProjectId("opaque-editor"),
        ProfileId(profile),
        SessionDisplayIdentity("editor", profile, "regular", 7),
        state,
        datetime.now(UTC),
    )


class _Sessions:
    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list_sessions(self):
        return (self.record,)


class _Store:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    #: How many writes to refuse, for the "sent but not recorded" interleaving.
    refuse_writes: int = 0

    async def remember(self, session_id, *, chat_id: int, message_id: int) -> None:
        if self.refuse_writes:
            self.refuse_writes -= 1
            raise RuntimeError("the database is busy")
        self.rows[str(session_id)] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "settled": False,
        }

    async def standing_for(self, session_id):
        from remote_agents.ports.trust_notifications import StandingTrustQuestion

        row = self.rows.get(str(session_id))
        return (
            None
            if row is None
            else StandingTrustQuestion(
                session_id, row["chat_id"], row["message_id"], row["settled"]
            )
        )

    async def unsettled(self):
        from remote_agents.ports.trust_notifications import StandingTrustQuestion

        return tuple(
            StandingTrustQuestion(SessionId.parse(key), row["chat_id"], row["message_id"], False)
            for key, row in self.rows.items()
            if not row["settled"]
        )

    async def settle(self, session_id) -> None:
        self.rows[str(session_id)]["settled"] = True


class _View:
    chat_id = 11

    def __init__(self, *, refuse: int = 0, schedule: list[bool] | None = None) -> None:
        self.sent: list[dict] = []
        self.amended: list[tuple[int, dict]] = []
        self._refuse = refuse
        #: Per-send refusal flags, for the tests that need an outage to *end* and then a
        #: later, separate one to begin. A bare count cannot express that.
        self._schedule = list(schedule or [])

    #: Sessions whose sends are refused, by the text that names them. Lets a pass have one
    #: session refused and another delivered, which is the only way to express DEC-049's
    #: "something else got through" clause.
    poison: str = ""

    async def send_apart(self, bot, arguments):
        del bot
        if self.poison and self.poison in arguments["text"]:
            raise BadRequest("chat not found")
        if self._schedule:
            if self._schedule.pop(0):
                raise BadRequest("chat not found")
        elif self._refuse:
            self._refuse -= 1
            raise BadRequest("chat not found")
        self.sent.append(arguments)
        return 100 + len(self.sent)

    #: How many amendments to refuse before letting one through. The keyboard attach is an
    #: amendment too, so this is how a "sent but not decorated" message is produced.
    refuse_amends: int = 0

    async def amend_apart(self, bot, message_id, arguments):
        del bot
        if self.refuse_amends:
            self.refuse_amends -= 1
            raise BadRequest("message could not be edited")
        self.amended.append((message_id, arguments))
        return True

    @property
    def settlements(self) -> list[dict]:
        """The amendments that *answer* the question, not the one that attaches its keyboard.

        Delivery is send-then-mint-then-attach, so the keyboard arrives as an amendment too.
        Counting raw amendments would therefore count the ask as an answer.
        """
        return [
            arguments
            for _, arguments in self.amended
            if "Trusted" in arguments["text"] or "Closed without trusting" in arguments["text"]
        ]


class _Callbacks:
    def __init__(self) -> None:
        self.minted = 0

    def create(self, action, entity, owner_id, chat_id, message_id, *, mutation=False):
        del action, entity, owner_id, chat_id, message_id, mutation
        self.minted += 1
        return f"c1_{'x' * 20}{self.minted:04d}"


def _notifier(sessions, store, view, callbacks=None) -> TrustNotifier:
    notifier = TrustNotifier(
        sessions=sessions,
        store=store,
        view=view,
        callbacks=callbacks or _Callbacks(),
        owner_user_id=7,
        # The real mapping, read off the registry rather than restated: a literal here would
        # agree with the shipped verticals on the day it was written and answer to nothing
        # afterwards, which is exactly the failure the frozenset it replaced had.
        trust_dialogs=profile_trust_dialogs(),
    )
    notifier.attach(object())
    return notifier


async def test_one_message_per_session_however_many_passes_run() -> None:
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)

    for _ in range(3):
        await notifier.pass_once()

    assert len(view.sent) == 1, "the question was asked more than once"


async def test_a_session_that_became_ready_amends_the_message_in_place() -> None:
    """DEC-034: a repeat is amended, not re-sent — the owner's phone buzzes once."""
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()

    sessions.record = replace(sessions.record, state=SessionState.RUNNING)
    await notifier.pass_once()

    assert len(view.sent) == 1, "the answer arrived as a second message"
    assert len(view.settlements) == 1
    assert "Trusted" in view.settlements[0]["text"]


async def test_a_settled_question_is_amended_exactly_once() -> None:
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()
    sessions.record = replace(sessions.record, state=SessionState.ENDED)

    await notifier.pass_once()
    await notifier.pass_once()

    assert len(view.settlements) == 1, "a settled row was amended again"


async def test_a_declined_session_amends_to_the_closed_wording() -> None:
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()

    sessions.record = replace(sessions.record, state=SessionState.ENDED)
    await notifier.pass_once()

    assert "Closed without trusting." in view.settlements[0]["text"]


async def test_a_whole_pass_that_got_nothing_through_records_no_strike(caplog) -> None:
    """DEC-049's load-bearing clause, and the one a bare three-strike counter gets wrong.

    When *nothing* got through, the refusals are evidence about an outage rather than about
    any one session. At a five-second cadence a bare counter turns a fifteen-second Telegram
    hiccup into permanent silence for every standing question — which is the failure DEC-049
    was written to prevent, and it is sharper here than for the activity pass because this
    message is short and structural, so almost every refusal it can see is an outage.
    """
    sessions, store, view = _Sessions(_record()), _Store(), _View(refuse=99)
    notifier = _notifier(sessions, store, view)

    with caplog.at_level("WARNING"):
        for _ in range(6):
            await notifier.pass_once()

    assert "giving up" not in caplog.text, "an outage abandoned a session"

    view._refuse = 0
    await notifier.pass_once()
    assert len(view.sent) == 1, "the question was never asked once the outage ended"


async def test_a_session_refused_while_others_get_through_is_abandoned(caplog) -> None:
    """The other half: refusals that *are* about the session still reach the bound."""
    poisoned = _record()

    def _healthy(n: int) -> SessionRecord:
        record = _record()
        object.__setattr__(record, "session_id", SessionId.new())
        object.__setattr__(
            record, "display", SessionDisplayIdentity(f"ok{n}", "claude", "regular", n)
        )
        return record

    class _Growing:
        """A fresh untrusted session each pass, so every pass has a delivery in it.

        Needed because a trust question is delivered *once per session ever*: after the first
        pass a standing row makes `_ask` return early, so a fixture with one healthy session
        supplies a delivery on pass 1 and none afterwards — and under DEC-049 that means one
        strike, not three. The decision's accepted consequence is that a lone refusing session
        on a quiet chat is retried rather than abandoned; reaching the bound requires the chat
        to be demonstrably reachable on each of the three passes.
        """

        def __init__(self) -> None:
            self.records = [poisoned]

        async def list_sessions(self):
            self.records.append(_healthy(len(self.records)))
            return tuple(self.records)

    store, view = _Store(), _View()
    view.poison = "editor"
    notifier = _notifier(_Growing(), store, view)

    with caplog.at_level("WARNING"):
        for _ in range(4):
            await notifier.pass_once()

    assert len(view.sent) >= 3, "the healthy sessions were not delivered"
    assert "giving up" in caplog.text
    assert sum("giving up" in r.message for r in caplog.records) == 1, (
        "the journal line is written once, not on every pass"
    )


async def test_an_outage_that_ends_does_not_spend_the_abandon_budget() -> None:
    """Consecutive, not cumulative, and the difference needs two *separate* outages to show.

    A single outage cannot distinguish the two rules: once the send succeeds the row exists
    and the session is never asked again, so a cumulative counter is never consulted a third
    time. This asks twice, with the outage ending in between — two refusals, a success, the
    session answered, then the *same* session untrusted again and one further refusal. That
    is three refusals in total and only one in a row. A cumulative counter abandons here; a
    consecutive one asks again.

    Written this way because the first version of it was vacuous: removing the reset changed
    nothing it could see.
    """
    sessions, store = _Sessions(_record()), _Store()
    view = _View(schedule=[True, True, False, True, False])
    notifier = _notifier(sessions, store, view)

    await notifier.pass_once()  # refused (1 in a row)
    await notifier.pass_once()  # refused (2 in a row)
    await notifier.pass_once()  # sent — the outage has ended
    sessions.record = replace(sessions.record, state=SessionState.RUNNING)
    await notifier.pass_once()  # answered, so the row settles
    sessions.record = replace(sessions.record, state=SessionState.UNTRUSTED)
    await notifier.pass_once()  # refused again (1 in a row, 3 in total)
    await notifier.pass_once()  # must ask again

    assert len(view.sent) == 2, (
        "the third refusal abandoned a session whose earlier outage had already ended, so "
        "the counter is cumulative rather than consecutive"
    )


async def test_a_pass_before_the_bot_is_attached_sends_nothing_and_forgets_nothing() -> None:
    """The composition root builds this long before there is a Telegram to speak through."""
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = TrustNotifier(
        sessions=sessions,
        store=store,
        view=view,
        callbacks=_Callbacks(),
        owner_user_id=7,
        trust_dialogs=profile_trust_dialogs(),
    )

    await notifier.pass_once()
    assert view.sent == []

    notifier.attach(object())
    await notifier.pass_once()
    assert len(view.sent) == 1


@pytest.mark.parametrize("state", [SessionState.RUNNING, SessionState.FAILED])
async def test_a_session_that_was_never_untrusted_is_never_asked(state: SessionState) -> None:
    sessions, store, view = _Sessions(_record(state)), _Store(), _View()

    await _notifier(sessions, store, view).pass_once()

    assert view.sent == []
    assert store.rows == {}


async def test_a_send_whose_row_could_not_be_written_is_not_sent_again() -> None:
    """The duplicate-message interleaving: the message landed, the bookkeeping did not.

    `remember` runs after the send, so a write that fails leaves no row — and a pass that
    read the store alone would see "never asked" and send the owner a second copy of a
    question they are already looking at. What is owed is the *rest* of the delivery, not
    another message.
    """
    sessions, view = _Sessions(_record()), _View()
    store = _Store()
    store.refuse_writes = 1
    notifier = _notifier(sessions, store, view)

    await notifier.pass_once()
    await notifier.pass_once()

    assert len(view.sent) == 1, "the owner was sent the same question twice"
    assert store.rows, "the row was never written, so a restart would ask again"


async def test_a_question_whose_keyboard_failed_gets_its_buttons_on_the_next_pass() -> None:
    """The stranding interleaving, and it is the worse of the two.

    `remember` succeeds and the keyboard amend fails, so the row says "already asked" while
    the message on the owner's phone has no buttons on it. A pass that trusted the row alone
    would skip this session forever: a question that can be read and not answered, with no
    retry and nothing in the journal saying so.
    """
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    view.refuse_amends = 1
    notifier = _notifier(sessions, store, view)

    await notifier.pass_once()
    assert view.amended == [], "the keyboard attach was supposed to fail"

    await notifier.pass_once()

    assert len(view.sent) == 1, "a retry of the keyboard re-sent the whole message"
    assert view.amended, "the question is still on the owner's phone with no buttons"
    assert "Trust this project" in str(view.amended[0][1]["reply_markup"])


async def test_one_session_failing_does_not_defer_every_other_session_s_answer() -> None:
    """Per-session isolation in the settle loop. One refusing chat is not a stalled pass."""
    first, second = _record(), _record()
    object.__setattr__(second, "session_id", SessionId.new())

    class _Two:
        def __init__(self):
            self.records = [first, second]

        async def list_sessions(self):
            return tuple(self.records)

    sessions, store, view = _Two(), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()
    assert len(view.sent) == 2

    sessions.records = [
        replace(first, state=SessionState.RUNNING),
        replace(second, state=SessionState.RUNNING),
    ]
    view.refuse_amends = 1
    await notifier.pass_once()

    assert len(view.settlements) == 1, "one session's failure took the other's answer with it"


async def test_a_question_already_on_the_owner_s_screen_is_not_sent_as_a_message() -> None:
    """The bot's own launch reply *is* the question, so the pass must stand down.

    Without this the pass finds an UNTRUSTED record with no standing row five seconds later
    and sends the same two buttons again — the "never sent twice" property failing on the one
    path where the owner is certainly looking at the first copy.
    """
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)

    notifier.note_asked_on_screen(sessions.record.session_id)
    await notifier.pass_once()
    await notifier.pass_once()

    assert view.sent == [], "the owner got a message for a question already on their screen"


def test_the_trust_question_s_buttons_are_never_adopted_as_the_live_view() -> None:
    """A message sent apart from the live view must not become it.

    Adopting one makes the next render draw a screen *over* the question, and the notifier's
    own later amendment then paints the settled text back over that screen. The vulnerable
    state — a chat with no recorded anchor and a notification already in it — is more
    characteristic here than for the activity notification, not less: an owner who launches
    only from the local surface may never have pressed a bot screen, so the trust question can
    be the one and only message in their chat.
    """
    from remote_agents.adapters.telegram.service import _SENT_APART_ACTIONS

    assert {"session.trust", "session.decline"} <= _SENT_APART_ACTIONS


@pytest.mark.asyncio
async def test_the_notification_gates_on_the_profile_and_the_detail_screen_on_the_pane() -> None:
    """The two surfaces do **not** check the same thing, and that asymmetry is deliberate.

    A regression pin rather than a fix: the code was already right and its docstring was not.
    `service._awaiting_trust` claimed the state gate was "the detail screen agreeing with its
    sibling rather than a new rule", which is true of the *state* filter -- this pass skips any
    record that is not `UNTRUSTED` -- and was read as covering the pane read too. It does not:
    the notification offers Trust on `answerable(profile_id, ...)` alone, with no capture taken.

    **Why it must stay that way.** The detail screen is re-rendered on demand, so a capture
    taken as it draws describes the pane the owner is looking at. A notification is sent once
    and outlives its own read by any amount of time, so a pane gate here would bind the button
    to an observation that was already stale when it was minted. The press is protected where it
    can be: `answer_trust` re-reads the pane before sending anything, and since BL-053's second
    finding it reports a refusal as a refusal instead of returning what success returns.

    So this asserts both halves. If someone "fixes" the asymmetry by taking a capture here, the
    first assertion fails and this docstring is what they read.
    """

    class _CountingSessions(_Sessions):
        def __init__(self, record) -> None:
            super().__init__(record)
            self.trust_state_calls = 0

        async def trust_state(self, session_id):
            del session_id
            self.trust_state_calls += 1
            from remote_agents.domain.trust import TrustState

            return TrustState.UNKNOWN

    sessions = _CountingSessions(_record(profile="claude"))
    view = _View()
    await _notifier(sessions, _Store(), view).pass_once()

    assert sessions.trust_state_calls == 0, (
        "the notification takes no capture -- a read here is stale before the owner sees it"
    )
    offered = [
        button.text
        for _, arguments in view.amended
        for row in arguments["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any("Trust this project" in label for label in offered), (
        "an answerable profile is still offered the answer; the profile is this surface's gate"
    )


@pytest.mark.asyncio
async def test_the_notification_still_shares_the_state_filter_it_was_credited_with() -> None:
    """The half of the claim that *was* true, pinned so the correction does not overshoot.

    Correcting "the two surfaces agree" must not turn into "the two surfaces share nothing".
    They share the state filter, and that is the whole of what this asserts.

    **It deliberately claims nothing about DEC-081.** An earlier draft of this docstring said
    the filter "can no longer be conjured", which this test does not exercise and which is not
    true of the code today -- DEC-081 is a recorded decision with no implementation yet, and
    inside `reconcile._LATE_DIALOG_WINDOW` a pane may still set `untrusted`. A test docstring
    making a claim its body never checks is the same defect one layer down, so the claim is
    gone rather than restated.
    """
    view = _View()
    await _notifier(_Sessions(_record(state=SessionState.RUNNING)), _Store(), view).pass_once()

    assert view.sent == [], "a running session is not asking, so no question is sent about it"
