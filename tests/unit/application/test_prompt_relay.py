"""`PromptRelay` decides send, queue or refuse, and delivers a queued message on "finished".

The owner's rulings (DEC-099): typed now if the agent is idle; otherwise queued -- one per session,
the newest replacing an older one -- and delivered after the session's next "finished" event, when
the idle check runs again. An agent with no "finished" event this project drains refuses rather
than queues; that is read off what the composition declares, never off a provider's name.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database, open_ui_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.queued_prompt_store import SQLiteQueuedPromptStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.prompt_relay import PromptRelay
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.message_relay import RelayOutcome
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason

_QUEUES = frozenset({"claude"})
"""The profiles this test's composition says have a drained "finished" event."""


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.domain = open_database(tmp_path / "sessions.sqlite3", migrations=MIGRATIONS)
        self.ui = open_ui_database(tmp_path / "ui.sqlite3")
        self.sessions = SQLiteSessionStore(self.domain)
        self.queue = SQLiteQueuedPromptStore(self.ui)
        self.terminal = FakeTerminal()
        self.relay = PromptRelay(
            self.terminal,
            self.queue,
            self.sessions,
            queues_for=lambda profile: str(profile) in _QUEUES,
        )

    def session(self, profile: str = "claude", state: SessionState = SessionState.RUNNING):
        session_id = SessionId.new()
        record = SessionRecord(
            session_id,
            ProjectId("p"),
            ProfileId(profile),
            SessionDisplayIdentity("p", profile, "regular", 1),
            state,
            datetime.now(UTC),
        )
        asyncio.run(self.sessions.save(record))
        return session_id

    def arm(self, *deliveries: PromptDelivery) -> None:
        self.terminal.prompt_deliveries.extend(deliveries)

    def close(self) -> None:
        self.domain.close()
        self.ui.close()


@pytest.fixture
def world(tmp_path: Path):
    built = World(tmp_path)
    try:
        yield built
    finally:
        built.close()


_BUSY = PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY)
_SENT = PromptDelivery(PromptOutcome.SENT)


def test_an_idle_session_is_sent_to_and_nothing_is_queued(world) -> None:
    session = world.session()
    world.arm(_SENT)

    result = asyncio.run(world.relay.submit(session, "hello"))

    assert result.outcome is RelayOutcome.SENT
    assert world.relay.pending(session) is None
    assert world.terminal.prompts == [(session, "hello")]


@pytest.mark.parametrize("reason", [PromptReason.BUSY, PromptReason.DIALOG])
def test_a_working_or_asking_session_queues_the_message(world, reason) -> None:
    session = world.session()
    world.arm(PromptDelivery(PromptOutcome.REFUSED, reason))

    result = asyncio.run(world.relay.submit(session, "hello"))

    assert (result.outcome, result.reason) == (RelayOutcome.QUEUED, reason)
    assert world.relay.pending(session).text == "hello"


def test_a_second_message_replaces_the_waiting_one_and_says_so(world) -> None:
    session = world.session()
    world.arm(_BUSY, _BUSY)

    first = asyncio.run(world.relay.submit(session, "first"))
    second = asyncio.run(world.relay.submit(session, "second"))

    assert (first.replaced, second.replaced) == (False, True)
    assert world.relay.pending(session).text == "second"


@pytest.mark.parametrize(
    "reason",
    [PromptReason.EMPTY, PromptReason.SHELL, PromptReason.MENU, PromptReason.NO_COMPOSER,
     PromptReason.TMUX_ERROR, PromptReason.COMPOSING, PromptReason.UNRECOGNISED,
     PromptReason.KEYS_BUSY],
)  # fmt: skip
def test_a_refusal_waiting_cannot_fix_is_refused_not_queued(world, reason) -> None:
    """COMPOSING, UNRECOGNISED and KEYS_BUSY included: they can sit on an idle agent that never
    finishes again, and a message queued behind one would fire hours later, out of context."""
    session = world.session()
    world.arm(PromptDelivery(PromptOutcome.REFUSED, reason))

    result = asyncio.run(world.relay.submit(session, "hello"))

    assert (result.outcome, result.reason) == (RelayOutcome.REFUSED, reason)
    assert world.relay.pending(session) is None


def test_a_provider_with_no_finished_event_refuses_instead_of_queueing(world) -> None:
    session = world.session(profile="cursor-agent")
    world.arm(_BUSY)

    result = asyncio.run(world.relay.submit(session, "hello"))

    assert (result.outcome, result.reason) == (RelayOutcome.REFUSED, PromptReason.BUSY)
    assert world.relay.pending(session) is None


def test_a_session_that_is_not_running_is_refused_without_asking_the_terminal(world) -> None:
    session = world.session(state=SessionState.ENDED)

    result = asyncio.run(world.relay.submit(session, "hello"))

    assert (result.outcome, result.reason) == (RelayOutcome.REFUSED, PromptReason.NOT_RUNNING)
    assert world.terminal.prompts == []


def test_a_finished_event_delivers_the_waiting_message(world) -> None:
    session = world.session()
    world.arm(_BUSY, _SENT)
    asyncio.run(world.relay.submit(session, "hello"))

    result = asyncio.run(world.relay.retry(session))

    assert result.outcome is RelayOutcome.SENT
    assert world.relay.pending(session) is None
    assert world.terminal.prompts[-1] == (session, "hello")


def test_a_retry_that_is_refused_again_keeps_the_message_waiting(world) -> None:
    session = world.session()
    world.arm(_BUSY, _BUSY)
    asyncio.run(world.relay.submit(session, "hello"))

    result = asyncio.run(world.relay.retry(session))

    assert result.outcome is RelayOutcome.QUEUED
    assert world.relay.pending(session).text == "hello"


def test_an_unconfirmed_delivery_is_never_retried(world) -> None:
    session = world.session()
    world.arm(_BUSY, PromptDelivery(PromptOutcome.UNCONFIRMED, PromptReason.SUBMIT_NOT_SEEN))
    asyncio.run(world.relay.submit(session, "hello"))

    result = asyncio.run(world.relay.retry(session))

    assert result.outcome is RelayOutcome.UNCONFIRMED
    assert world.relay.pending(session) is None


def test_a_retry_with_nothing_waiting_asks_nothing(world) -> None:
    session = world.session()

    assert asyncio.run(world.relay.retry(session)) is None
    assert world.terminal.prompts == []


def test_cancel_removes_the_waiting_message(world) -> None:
    session = world.session()
    world.arm(_BUSY)
    asyncio.run(world.relay.submit(session, "hello"))

    assert world.relay.cancel(session) is True
    assert world.relay.pending(session) is None


def test_a_sent_message_drops_an_older_waiting_one(world) -> None:
    """Newest wins: an older message must not fire after a newer one was delivered."""
    session = world.session()
    world.arm(_BUSY, _SENT)
    asyncio.run(world.relay.submit(session, "older"))

    asyncio.run(world.relay.submit(session, "newer"))

    assert world.relay.pending(session) is None


@pytest.mark.parametrize("state", [SessionState.ENDED, SessionState.STOP_REQUESTED])
def test_the_sweep_drops_messages_for_sessions_that_stopped_or_ended(world, state) -> None:
    running = world.session()
    stopping = world.session()
    world.arm(_BUSY, _BUSY)
    asyncio.run(world.relay.submit(running, "stays"))
    asyncio.run(world.relay.submit(stopping, "goes"))
    from remote_agents.domain.state_machine import LifecycleEvent

    asyncio.run(world.sessions.record_event(stopping, LifecycleEvent.GRACEFUL_STOP_REQUESTED))
    if state is SessionState.ENDED:
        asyncio.run(world.sessions.record_event(stopping, LifecycleEvent.CLEANUP_CONFIRMED))
    assert asyncio.run(world.sessions.get(stopping)).state is state

    asyncio.run(world.relay.sweep())

    assert world.relay.pending(running).text == "stays"
    assert world.relay.pending(stopping) is None


class _TypingTerminal(FakeTerminal):
    """A terminal that runs `during` while it is typing -- the window a cancel can land in."""

    def __init__(self, during) -> None:
        super().__init__()
        self.during = during

    async def send_prompt(self, session_id, text):
        delivery = await super().send_prompt(session_id, text)
        if self.during is not None:
            self.during()
        return delivery


def _typing_world(world, during) -> PromptRelay:
    terminal = _TypingTerminal(during)
    terminal.prompt_deliveries = [_SENT]
    world.terminal = terminal
    return PromptRelay(
        terminal, world.queue, world.sessions, queues_for=lambda profile: str(profile) in _QUEUES
    )


def test_a_cancel_landing_while_the_retry_types_is_reported_as_overtaken(world) -> None:
    session = world.session()
    world.queue.queue(str(session), "hello")
    relay = _typing_world(world, lambda: world.queue.cancel(str(session)))

    result = asyncio.run(relay.retry(session))

    assert (result.outcome, result.overtaken) == (RelayOutcome.SENT, True)
    assert relay.pending(session) is None


def test_a_newer_message_landing_while_the_retry_types_is_overtaken_and_still_waits(world) -> None:
    session = world.session()
    world.queue.queue(str(session), "older")
    relay = _typing_world(world, lambda: world.queue.queue(str(session), "newer"))

    result = asyncio.run(relay.retry(session))

    assert (result.outcome, result.overtaken) == (RelayOutcome.SENT, True)
    assert relay.pending(session).text == "newer"


def test_a_retry_nobody_interrupted_is_not_overtaken(world) -> None:
    session = world.session()
    world.queue.queue(str(session), "hello")
    relay = _typing_world(world, None)

    assert asyncio.run(relay.retry(session)).overtaken is False


def test_a_retry_cancelled_mid_delivery_drops_the_message_rather_than_retype_it(world) -> None:
    """A deploy restart cancels the pass mid-paste; its claim must not be taken again later."""
    from datetime import timedelta

    session = world.session()
    world.queue.queue(str(session), "hello")

    def stop_the_service() -> None:
        raise asyncio.CancelledError

    relay = _typing_world(world, stop_the_service)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(relay.retry(session))

    later = datetime.now(UTC) + timedelta(minutes=5)
    assert world.queue.claim(str(session), now=later) is None, "it would be typed a second time"


# --- turn markers of sessions that are gone (BL-108) -----------------------------------------


class _Markers:
    def __init__(self, *sessions: str) -> None:
        self.present = set(sessions)
        self.ended: list[str] = []

    def start(self, session_id: str) -> None:
        self.present.add(session_id)

    def end(self, session_id: str) -> None:
        self.ended.append(session_id)
        self.present.discard(session_id)

    def started_at(self, session_id: str):
        return None

    def sessions(self) -> tuple[str, ...]:
        return tuple(sorted(self.present))


def test_the_sweep_ends_the_turn_markers_of_sessions_that_are_not_running(world) -> None:
    from remote_agents.domain.state_machine import LifecycleEvent

    running = world.session()
    stopping = world.session()
    asyncio.run(world.sessions.record_event(stopping, LifecycleEvent.GRACEFUL_STOP_REQUESTED))
    unknown = str(SessionId.new())
    markers = _Markers(str(running), str(stopping), unknown, "not-a-session-id")
    relay = PromptRelay(
        world.terminal,
        world.queue,
        world.sessions,
        queues_for=lambda profile: str(profile) in _QUEUES,
        turn_markers=markers,
    )

    asyncio.run(relay.sweep())

    assert markers.present == {str(running)}
    assert sorted(markers.ended) == sorted([str(stopping), unknown, "not-a-session-id"])


def test_the_sweep_without_markers_is_unchanged(world) -> None:
    asyncio.run(world.relay.sweep())
