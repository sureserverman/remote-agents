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
        #: Message ids actually taken out of the chat.
        self.discarded: list[int] = []
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

    #: How many deletions Telegram *declines* -- answered `False`, not raised. That is the
    #: real 48-hour case: the message survives and the caller must stop treating it as
    #: standing, which is what the amend fallback is for.
    refuse_discards: int = 0

    #: How many deletions fail outright. A different thing from a refusal, and the pass has to
    #: keep going for every other session when one does it.
    raise_discards: int = 0

    async def amend_apart(self, bot, message_id, arguments):
        del bot
        if self.refuse_amends:
            self.refuse_amends -= 1
            raise BadRequest("message could not be edited")
        self.amended.append((message_id, arguments))
        return True

    async def discard(self, bot, message_id: int) -> bool:
        del bot
        if self.raise_discards:
            self.raise_discards -= 1
            raise BadRequest("message to delete not found")
        if self.refuse_discards:
            self.refuse_discards -= 1
            return False
        self.discarded.append(message_id)
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


async def test_a_session_that_became_ready_has_its_question_taken_out_of_the_chat() -> None:
    """DEC-082: the question disappears once answered, rather than becoming a receipt.

    It used to be amended in place to *Trusted. The agent is running.* with a way into the
    session — DEC-034's rule for an amendment — and the message then stayed in the chat for
    good. The owner asked for it to go: the sessions list is already the record of what
    exists, so a trusted session needs no standing message to say so.
    """
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()
    asked = view.sent[0]

    sessions.record = replace(sessions.record, state=SessionState.RUNNING)
    await notifier.pass_once()

    assert len(view.sent) == 1, "the answer arrived as a second message"
    assert view.discarded == [101], "the answered question is still in the chat"
    assert view.settlements == [], "it was amended into a receipt instead of removed"
    assert "Waiting to be trusted" in asked["text"], "sanity: that was the question"
    assert store.rows[str(_SESSION)]["settled"] is True


async def test_a_settled_question_is_removed_exactly_once() -> None:
    """The durable row is what stops a second pass trying to delete a message again."""
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()
    sessions.record = replace(sessions.record, state=SessionState.ENDED)

    await notifier.pass_once()
    await notifier.pass_once()

    assert view.discarded == [101], "a settled row was acted on again"


async def test_a_declined_session_has_its_question_removed_too() -> None:
    """Both endings, one rule — which is why the owner chose *delete both*.

    A declined session used to leave *Closed without trusting.* standing. It is the only
    trace that the session ever existed, which is an argument for keeping it; the argument
    that won is that the decline press already answers with that sentence as a transient
    notice, so the persistent copy adds nothing but accumulation.
    """
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()

    sessions.record = replace(sessions.record, state=SessionState.ENDED)
    await notifier.pass_once()

    assert view.discarded == [101]
    assert view.settlements == [], "the declined ending was left standing in the chat"


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
    view.raise_discards = 1
    await notifier.pass_once()

    assert len(view.discarded) == 1, "one session's failure took the other's answer with it"


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


async def test_a_question_too_old_to_delete_is_amended_so_it_stops_asking() -> None:
    """The 48-hour case, and the reason `render_trust_settled` is still here.

    Telegram refuses to delete a message past 48 hours, so a question answered days late
    cannot be removed. The wrong answer is to settle the row and walk away: that leaves the
    owner a message still headed *Waiting to be trusted*, with two live answers on it, for a
    session that stopped waiting long ago. So a refused deletion falls back to the amendment
    — the message stays, but it stops asking.

    `discard` answering `False` rather than raising is what makes this reachable; that is the
    distinction its own docstring draws between "deleted" and "refused".
    """
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()

    view.refuse_discards = 1
    sessions.record = replace(sessions.record, state=SessionState.RUNNING)
    await notifier.pass_once()

    assert view.discarded == [], "Telegram refused, so nothing was removed"
    assert len(view.settlements) == 1, "a refused deletion must still stop the message asking"
    assert "Trusted" in view.settlements[0]["text"]
    assert store.rows[str(_SESSION)]["settled"] is True, (
        "the row must settle on both paths, or every pass retries this for as long as the "
        "record survives"
    )


async def test_a_refused_deletion_is_not_retried_forever() -> None:
    """Settling on the fallback path is what bounds it, and this is why that matters.

    A message Telegram will not delete will still not be deletable on the next pass, and the
    pass runs every five seconds. Without the row settling on the fallback path, each one
    would attempt the delete and then rewrite the same amendment, for as long as the record
    exists.
    """
    sessions, store, view = _Sessions(_record()), _Store(), _View()
    notifier = _notifier(sessions, store, view)
    await notifier.pass_once()

    # Refused on *every* pass, not just the first -- a message too old to delete stays too
    # old. With only the first refused, later passes would simply succeed and the retry loop
    # this test exists for could never appear.
    view.refuse_discards = 9
    sessions.record = replace(sessions.record, state=SessionState.RUNNING)
    await notifier.pass_once()
    await notifier.pass_once()
    await notifier.pass_once()

    assert len(view.settlements) == 1, "the fallback amendment was rewritten on later passes"
