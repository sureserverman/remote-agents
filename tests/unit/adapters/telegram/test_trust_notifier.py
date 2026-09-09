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

    async def remember(self, session_id, *, chat_id: int, message_id: int) -> None:
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

    async def send_apart(self, bot, arguments):
        del bot
        if self._schedule:
            if self._schedule.pop(0):
                raise BadRequest("chat not found")
        elif self._refuse:
            self._refuse -= 1
            raise BadRequest("chat not found")
        self.sent.append(arguments)
        return 100 + len(self.sent)

    async def amend_apart(self, bot, message_id, arguments):
        del bot
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


async def test_three_consecutive_refusals_abandon_the_question(caplog) -> None:
    """DEC-049. A chat that will not take the message is not hammered forever."""
    sessions, store, view = _Sessions(_record()), _Store(), _View(refuse=99)
    notifier = _notifier(sessions, store, view)

    for _ in range(3):
        await notifier.pass_once()
    with caplog.at_level("WARNING"):
        await notifier.pass_once()

    assert view.sent == []
    assert "giving up" in caplog.text
    assert len(caplog.records) == 1, "the journal line is written once, not on every pass"


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
        sessions=sessions, store=store, view=view, callbacks=_Callbacks(), owner_user_id=7
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
