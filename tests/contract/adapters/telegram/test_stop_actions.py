from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from uuid import UUID

import pytest
from backends import SessionUseCaseDouble, backend_for
from stop_results import (
    a_clean_stop,
    a_reader_for,
    a_stop_that_did_not_take,
    a_verified_force_stop,
)

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import (
    GRACEFUL_TIMEOUT,
    UNKNOWN_SESSION,
    stop_failure,
)
from remote_agents.application.stops import execute_stop
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


class FakeService:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def graceful_stop(self, _command):
        self.actions.append("graceful")
        return a_clean_stop()

    async def cleanup(self, _command) -> None:
        self.actions.append("cleanup")

    async def force_stop(self, _command):
        self.actions.append("force")
        return a_verified_force_stop()


class Record:
    # Mirrors SessionRecord's tenth field. A fake missing it duck-types the record
    # everywhere except the one branch DEC-020 added, which is the branch that offers a
    # destructive action.
    orphan_provenance = None

    def __init__(self, session_id: SessionId, state: SessionState, profile_id: ProfileId) -> None:
        self.session_id = session_id
        self.state = state
        self.profile_id = profile_id


def test_stop_actions_require_the_right_confirmation_strength_and_claim_once() -> None:
    callbacks = CallbackStateStore()
    controller = StopController(callbacks)
    session = SessionId(UUID(int=1))
    graceful = controller.offer(
        session, ProfileId("claude"), SessionState.RUNNING, None, "graceful", 7, 11
    )
    force = controller.offer(
        session, ProfileId("claude"), SessionState.RUNNING, None, "force", 7, 11
    )
    callbacks.bind_pending(11, 1)

    claimed = controller.claim(graceful, 7, 11, 1)
    assert claimed is not None
    assert (claimed.action, claimed.session_id, claimed.profile_id) == (
        "graceful",
        session,
        ProfileId("claude"),
    )
    assert controller.claim(graceful, 7, 11, 1) is None
    # An unconfirmed force is not claimable at all: it only opens the confirmation screen,
    # whose own button is a separate token carrying a separate action.
    assert controller.claim(force, 7, 11, 1) is None
    confirmed = controller.offer_confirmed_force(
        session, ProfileId("claude"), SessionState.RUNNING, None, 7, 11
    )
    assert confirmed is not None
    callbacks.bind_pending(11, 1)
    claimed_force = controller.claim(confirmed, 7, 11, 1)
    assert claimed_force is not None and claimed_force.action == "force"
    assert controller.claim(confirmed, 7, 11, 1) is None


def test_cleanup_only_exists_for_preserved_sessions() -> None:
    controller = StopController(CallbackStateStore())
    session = SessionId(UUID(int=1))

    assert (
        controller.offer(session, ProfileId("claude"), SessionState.RUNNING, None, "cleanup", 7, 11)
        is None
    )
    assert controller.offer(
        session, ProfileId("claude"), SessionState.PRESERVED, None, "cleanup", 7, 11
    )


@pytest.mark.asyncio
async def test_force_stop_is_available_for_a_failed_session_that_needs_cleanup() -> None:
    callbacks = CallbackStateStore()
    controller = StopController(callbacks)
    session = SessionId(UUID(int=1))
    token = controller.offer(session, ProfileId("codex"), SessionState.FAILED, None, "force", 7, 11)

    assert token is not None
    confirmed = controller.offer_confirmed_force(
        session, ProfileId("codex"), SessionState.FAILED, None, 7, 11
    )
    assert confirmed is not None
    callbacks.bind_pending(11, 1)
    request = controller.claim(confirmed, 7, 11, 1)
    assert request is not None
    service = FakeService()

    forced = await execute_stop(
        request.action,
        request.session_id,
        sessions=service,
        read_record=a_reader_for(Record(session, SessionState.FAILED, ProfileId("codex"))),
        profile_id=request.profile_id,
    )
    assert forced.dispatched
    assert service.actions == ["force"]


@pytest.mark.asyncio
async def test_claimed_action_rechecks_current_state_before_typed_service_dispatch() -> None:
    callbacks = CallbackStateStore()
    controller = StopController(callbacks)
    session = SessionId(UUID(int=1))
    token = controller.offer(
        session, ProfileId("claude"), SessionState.RUNNING, None, "graceful", 7, 11
    )
    callbacks.bind_pending(11, 1)
    assert token is not None
    claimed = controller.claim(token, 7, 11, 1)
    assert claimed is not None
    service = FakeService()

    result = await execute_stop(
        claimed.action,
        claimed.session_id,
        sessions=service,
        read_record=a_reader_for(Record(session, SessionState.PRESERVED, ProfileId("claude"))),
        profile_id=claimed.profile_id,
    )
    # `.dispatched`, not the outcome's own truthiness. `StopOutcome` inherits the retired
    # `StopResult`'s poison-pill `__bool__` for exactly this line: a dataclass instance is
    # unconditionally truthy, so `not result` would silently pass on every outcome.
    assert not result.dispatched
    assert service.actions == []
    mismatched = await execute_stop(
        claimed.action,
        claimed.session_id,
        sessions=service,
        read_record=a_reader_for(Record(session, SessionState.RUNNING, ProfileId("codex"))),
        profile_id=claimed.profile_id,
    )
    assert not mismatched.dispatched


def _stopped_boundary(*records: SessionRecord) -> PrivateBotBoundary:
    """A boundary whose list holds exactly `records` — what remains *after* the stop ran."""

    class _Launcher(SessionUseCaseDouble):
        async def list_sessions(self):
            return list(records)

        async def refresh_readiness(self) -> None:
            return None

    return build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
            sessions=_Launcher(),
        ),
    )


def _a_session(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=7)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        state,
        datetime(2026, 8, 10, tzinfo=UTC),
    )


def _another_session() -> SessionRecord:
    """A second session that outlives the one being stopped, so the landing has rows."""
    return SessionRecord(
        SessionId(UUID(int=8)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 2),
        SessionState.RUNNING,
        datetime(2026, 8, 10, tzinfo=UTC),
    )


def _labels(rendered) -> list[str]:
    return [button.text for row in rendered.keyboard for button in row]


@pytest.mark.asyncio
async def test_a_clean_stop_lands_on_list_naming_what_ended() -> None:
    """The request that started this: a stop should leave the owner where they can act next.

    The session is gone from the list because it ended, so the rows below the notice are the
    ones that are left — which is the screen the old dead end made them navigate to.
    """
    record = _a_session()
    boundary = _stopped_boundary()  # it ended, so it is no longer listed

    rendered = await boundary._stop_outcome_landing("graceful", record)

    assert rendered.text.startswith("Stopped Demo · Claude · regular · #1\n")
    assert "The session has ended." in rendered.text
    empty_list = "<b>Sessions</b> · 0 total · 0 active · 0 preserved\nNothing is running."
    assert empty_list in rendered.text
    assert "Back" not in _labels(rendered), "the list is the destination, not a stop on the way"


@pytest.mark.asyncio
async def test_a_graceful_timeout_lands_on_list_with_its_words_unchanged() -> None:
    """BL-008's rule, carried onto the list: the failure's words are passed through.

    `graceful_timeout` and `unknown_session` leave identical evidence in the record, so the
    sentence has to come from the observation rather than be re-derived from what is listed.
    Asserted against `stop_failure`'s own output, so re-wording it in the application layer
    cannot leave this test asserting a copy that has drifted.
    """
    failure = stop_failure(a_stop_that_did_not_take(GRACEFUL_TIMEOUT))
    assert failure is not None
    record = _a_session()
    boundary = _stopped_boundary(record)  # the stop did not take, so it is still listed

    rendered = await boundary._stop_outcome_landing("graceful", record, failure)

    assert rendered.text.startswith("Demo · Claude · regular · #1 is still running\n")
    assert escape(failure.summary) in rendered.text
    assert escape(failure.remedy) in rendered.text
    assert "Sessions 1/1" in rendered.text, "it did not take, so the session is still on the list"
    assert "Back" not in _labels(rendered)


@pytest.mark.asyncio
async def test_a_stop_that_failed_for_a_departed_session_lands_on_list_with_its_own_words() -> None:
    """The BL-008 branch: the record says gone, the observation says nothing was stopped.

    Reporting "Stopped X" here would assert an ending the observation contradicts, which is
    the reading DEC-006 forbids — so this branch keeps its own wording above the rows.
    """
    failure = stop_failure(a_stop_that_did_not_take(UNKNOWN_SESSION))
    assert failure is not None
    record = _a_session()
    boundary = _stopped_boundary()  # gone from the list, while the stop reported failure

    rendered = await boundary._stop_outcome_landing("graceful", record, failure)

    assert rendered.text.startswith("Demo · Claude · regular · #1 is no longer listed\n")
    assert escape(failure.summary) in rendered.text
    assert escape(failure.remedy) in rendered.text
    assert "Stopped" not in rendered.text, "nothing observed says this session was stopped"
    assert "Back" not in _labels(rendered)


@pytest.mark.asyncio
async def test_a_stop_outcome_lands_on_list_with_the_session_name_escaped() -> None:
    """The notice is escaped once, by the list. A display name is not markup we authored."""
    record = SessionRecord(
        SessionId(UUID(int=7)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("<b>Demo</b>", "Claude", "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 8, 10, tzinfo=UTC),
    )

    rendered = await _stopped_boundary()._stop_outcome_landing("graceful", record)

    assert "&lt;b&gt;Demo&lt;/b&gt;" in rendered.text
    assert "<b>Demo</b>" not in rendered.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "lead"),
    [
        ("graceful", "Stopped Demo · Claude · regular · #1"),
        ("cleanup", "Cleaned up Demo · Claude · regular · #1"),
        ("force", "Force stopped Demo · Claude · regular · #1"),
    ],
)
async def test_every_stop_action_lands_on_list_with_its_own_lead_line(action, lead) -> None:
    """All three end the same way and say different things, which is the point.

    A shared landing with a shared sentence would report a cleanup as a stop; the actions
    remove different things and the owner is told which one ran.
    """
    record = _a_session()

    ended_alone = await _stopped_boundary()._stop_outcome_landing(action, record)
    # And again with a survivor on the list, so the lead line is checked over rows as well as
    # over the empty state — the two branches of `_sessions_reply` render the notice
    # separately, and only one of them was covered when this was written.
    with_survivor = await _stopped_boundary(_another_session())._stop_outcome_landing(
        action, record
    )

    assert ended_alone.text.startswith(f"{lead}\n")
    assert "Nothing is running." in ended_alone.text
    assert with_survivor.text.startswith(f"{lead}\n")
    assert "Sessions 1/1" in with_survivor.text
    assert "Back" not in _labels(ended_alone)
    assert "Back" not in _labels(with_survivor)


class _MovedOnLauncher(SessionUseCaseDouble):
    """Lists the session in a state that no longer matches the one its token was offered at."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record
        self.stopped: list[str] = []

    async def list_sessions(self):
        return [] if self.record is None else [self.record]

    async def refresh_readiness(self) -> None:
        return None

    async def graceful_stop(self, _command):
        self.stopped.append("graceful")
        self.record = None
        return a_clean_stop()


@pytest.mark.asyncio
async def test_a_stop_refused_because_the_session_moved_on_lands_on_list() -> None:
    """The fourth outcome, and the only one that reports something the owner did not cause.

    The token was offered against RUNNING; by the time the thumb landed the session was
    PRESERVED, so the controller refuses to dispatch. It used to say "Open the list again to
    see where it is now" over a Back button — an instruction to navigate to the screen the
    owner now arrives on, so only the half that says what happened survives.
    """
    offered = _a_session(SessionState.RUNNING)
    moved_on = _a_session(SessionState.PRESERVED)
    launcher = _MovedOnLauncher(moved_on)
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),), sessions=launcher
        ),
    )
    token = boundary.stops.offer(
        offered.session_id, offered.profile_id, SessionState.RUNNING, None, "graceful", 7, 11
    )
    assert token is not None
    boundary.callbacks.bind_pending(11, 1)

    reply = await boundary._stop_reply("graceful", token, 1)

    assert launcher.stopped == [], "a refused stop dispatches nothing"
    assert reply["text"].startswith("That session moved on before this could run")
    assert "Open the list again" not in reply["text"]
    assert "Sessions 1/1" in reply["text"]
    labels = [
        unpadded(button.text) for row in reply["reply_markup"].inline_keyboard for button in row
    ]
    assert "Back" not in labels


@pytest.mark.asyncio
async def test_force_confirms_before_anything_lands_on_list() -> None:
    """Nothing above moves the one screen that stands in front of an irreversible action.

    The rule is unchanged — the destructive button must not be where the thumb already
    rests — but the layout satisfying it is not. Cancel used to come first because the row
    beneath was a lone Home nobody pressed. The bottom row is the navigation bar now, so
    Force stop is offered first and Cancel buffers it from the row the owner taps most.
    """
    record = _a_session()
    boundary = _stopped_boundary(record)
    token = boundary.stops.offer(
        record.session_id, record.profile_id, record.state, None, "force", 7, 11
    )
    assert token is not None
    boundary.callbacks.bind_pending(11, 1)

    reply = await boundary._stop_reply("force", token, 1)

    assert "Force stop" in reply["text"]
    assert "cannot be undone" in reply["text"]
    rows = [
        [unpadded(button.text) for button in row] for row in reply["reply_markup"].inline_keyboard
    ]
    assert rows[0] == ["Force stop"]
    assert rows[1] == ["Cancel"]


@pytest.mark.asyncio
async def test_a_repeated_stop_press_lands_on_list_rather_than_a_home_only_screen() -> None:
    """DEC-008 drops the repeat; where the answer is *drawn* is a separate question.

    The repeat is still not serviced — that is what the claim guarantees and it is unchanged.
    What changed is that saying so no longer costs the owner the screen: this was the last
    Home-only dead end a stop button could reach.
    """
    record = _a_session()
    launcher = _MovedOnLauncher(record)
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),), sessions=launcher
        ),
    )
    token = boundary.stops.offer(
        record.session_id, record.profile_id, record.state, None, "graceful", 7, 11
    )
    assert token is not None
    boundary.callbacks.bind_pending(11, 1)

    first = await boundary._stop_reply("graceful", token, 1)
    second = await boundary._stop_reply("graceful", token, 1)

    assert launcher.stopped == ["graceful"], "the repeat was dropped, not serviced twice"
    assert first["text"].startswith("Stopped ")
    assert second["text"].startswith("That action has already run.")
    labels = [
        unpadded(button.text) for row in second["reply_markup"].inline_keyboard for button in row
    ]
    # The list's own keyboard, not a lone dead end. Asserted by the empty list's own Launch
    # row rather than by the footer, which since the navigation bar reads the same three
    # destinations on every screen alike -- a signature that cannot distinguish the list
    # from anywhere else.
    assert "Sessions" in second["text"]
    assert labels == ["Sessions", "Launch"], "it answers on the list, whose way out is the bar"
