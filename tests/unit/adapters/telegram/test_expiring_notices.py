"""Messages that delete themselves: an early limits reset after the owner's chosen lifetime, and a
failed rollover once resolved or after an hour (owner's request, 2026-10-18).

Both notifiers used to `send_apart` and forget the message id, so every such message stayed in
the chat until the owner deleted it by hand.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from remote_agents.adapters.sqlite.expiring_notices import SQLiteExpiringNoticeStore
from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS, apply_migrations
from remote_agents.adapters.telegram.expiring_notices import (
    ExpiringMessages,
    InMemoryExpiringNoticeStore,
)
from remote_agents.adapters.telegram.limit_reset_notifications import LimitResetNotifier
from remote_agents.adapters.telegram.rollover_notifications import (
    NOTICE_LIFETIME,
    RolloverNotifier,
)
from remote_agents.application.rollover import RolloverReport, rollover_notice_settled
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow
from remote_agents.ports.expiring_notices import ExpiringNotice
from remote_agents.ports.rollover_store import Rollover

START = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class _View:
    """A recording `LiveView`: what was sent, and what was deleted."""

    chat_id = 4242

    def __init__(self, *, refuse_delete: bool = False) -> None:
        self.sent: dict[int, str] = {}
        self.deleted: list[int] = []
        self._refuse_delete = refuse_delete

    async def send_apart(self, bot: object, arguments: dict[str, object]) -> int:
        message_id = 100 + len(self.sent)
        self.sent[message_id] = str(arguments["text"])
        return message_id

    async def discard(self, bot: object, message_id: int) -> bool:
        if self._refuse_delete:
            return False
        self.deleted.append(message_id)
        return True


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def _limits(percent: float, *, at: datetime, resets: datetime) -> tuple[AgentLimits, ...]:
    return (
        AgentLimits(
            ProfileId("codex"),
            (UsageWindow("5h", percent, resets),),
            observed_at=at,
            stale_source=None,
        ),
    )


def _reset_notifier(view: _View, clock: _Clock, readings: list, choice: str, store=None):
    ticks = iter(readings)

    async def limits():
        return next(ticks)

    async def read_choice() -> str:
        return choice

    notifier = LimitResetNotifier(
        limits=limits,
        view=view,
        name_for=str.title,
        now=clock,
        choice=read_choice,
        notices=store,
    )
    notifier.attach(object())
    return notifier


def _wipe(start: datetime) -> list:
    """A baseline at 91%, then a wipe to 2% into a new window, then quiet readings."""
    old_reset = start + timedelta(hours=3)
    new_reset = start + timedelta(hours=5, minutes=10)
    quiet = [
        _limits(2.0, at=start + timedelta(minutes=10 * n), resets=new_reset) for n in range(2, 60)
    ]
    return [
        _limits(91.0, at=start, resets=old_reset),
        _limits(2.0, at=start + timedelta(minutes=10), resets=new_reset),
        *quiet,
    ]


# The early reset ----------------------------------------------------------------------


async def test_an_early_reset_message_is_deleted_an_hour_after_it_was_sent() -> None:
    view, clock = _View(), _Clock()
    notifier = _reset_notifier(view, clock, _wipe(START), "1h")

    await notifier.pass_once()
    clock.now = START + timedelta(minutes=10)
    await notifier.pass_once()
    assert list(view.sent.values()) == ["Codex limits were reset early — 5h 91% → 2%"]

    clock.now = START + timedelta(minutes=69)
    await notifier.pass_once()
    assert view.deleted == [], "not yet an hour"

    clock.now = START + timedelta(minutes=70)
    await notifier.pass_once()
    assert view.deleted == [100]


async def test_six_hours_keeps_the_message_for_six_hours() -> None:
    view, clock = _View(), _Clock()
    notifier = _reset_notifier(view, clock, _wipe(START), "6h")

    await notifier.pass_once()
    clock.now = START + timedelta(minutes=10)
    await notifier.pass_once()
    clock.now = START + timedelta(hours=6, minutes=9)
    await notifier.pass_once()
    assert view.deleted == []

    clock.now = START + timedelta(hours=6, minutes=10)
    await notifier.pass_once()
    assert view.deleted == [100]


async def test_off_sends_nothing_and_still_moves_the_baseline(caplog) -> None:
    view, clock = _View(), _Clock()
    notifier = _reset_notifier(view, clock, _wipe(START), "off")

    await notifier.pass_once()
    clock.now = START + timedelta(minutes=10)
    with caplog.at_level("INFO"):
        await notifier.pass_once()

    assert view.sent == {}
    assert "not told, the notice is off" in caplog.text


async def test_a_detection_is_journalled_with_its_source_and_the_reset_it_beat(caplog) -> None:
    view, clock = _View(), _Clock()
    notifier = _reset_notifier(view, clock, _wipe(START), "1h")

    await notifier.pass_once()
    clock.now = START + timedelta(minutes=10)
    with caplog.at_level("INFO"):
        await notifier.pass_once()

    assert "early limits reset detected for codex (source live): 5h 91% -> 2%" in caplog.text
    assert (START + timedelta(hours=3)).isoformat() in caplog.text


async def test_a_newer_reset_for_the_same_provider_replaces_the_older_message() -> None:
    view, clock = _View(), _Clock()
    first_new = START + timedelta(hours=5)
    second_new = START + timedelta(hours=5, minutes=40)
    readings = [
        _limits(91.0, at=START, resets=START + timedelta(hours=3)),
        _limits(2.0, at=START + timedelta(minutes=10), resets=first_new),
        _limits(80.0, at=START + timedelta(minutes=20), resets=first_new),
        _limits(1.0, at=START + timedelta(minutes=30), resets=second_new),
    ]
    notifier = _reset_notifier(view, clock, readings, "6h")

    for minutes in (0, 10, 20, 30):
        clock.now = START + timedelta(minutes=minutes)
        await notifier.pass_once()

    assert sorted(view.sent) == [100, 101]
    assert view.deleted == [100]


async def test_a_restart_still_deletes_the_message_it_sent_before() -> None:
    """The message id is in the UI store, so the next process removes it on time."""
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, UI_MIGRATIONS)
    store = SQLiteExpiringNoticeStore(connection)
    view, clock = _View(), _Clock()
    before = _reset_notifier(view, clock, _wipe(START), "1h", store)
    await before.pass_once()
    clock.now = START + timedelta(minutes=10)
    await before.pass_once()

    after = _reset_notifier(view, clock, _wipe(START + timedelta(hours=2)), "1h", store)
    clock.now = START + timedelta(minutes=70)
    await after.pass_once()

    assert view.deleted == [100]
    assert await store.standing("limit-reset") == ()


# The failed rollover ------------------------------------------------------------------


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("p"),
        ProfileId("claude"),
        SessionDisplayIdentity("p", "claude", "regular", 3, None),
        state,
        START,
    )


def _rollover(predecessor: SessionRecord, *, id: str = "r1", code: str = "adoption-timeout"):
    return Rollover(
        id=id,
        handoff_id="h-0123456789abcdef0123",
        predecessor_session_id=predecessor.session_id,
        successor_session_id=None,
        project_id=ProjectId("p"),
        profile_id=ProfileId("claude"),
        reason="workflow",
        plan=None,
        state=RolloverState.FAILED,
        failure_code=code,
        failure_detail=None,
        requested_at=START,
        updated_at=START,
    )


def _rollover_notifier(view: _View, clock: _Clock, settled=None):
    notifier = RolloverNotifier(
        view=view, project_name=lambda _: "remote-agents", now=clock, settled=settled
    )
    notifier.attach(object())
    return notifier


async def test_a_failed_rollover_message_is_deleted_after_an_hour() -> None:
    view, clock = _View(), _Clock()
    predecessor = _record()
    notifier = _rollover_notifier(view, clock)

    await notifier.notify(RolloverReport(_rollover(predecessor), predecessor))
    assert len(view.sent) == 1

    clock.now = START + NOTICE_LIFETIME - timedelta(seconds=1)
    await notifier.pass_once()
    assert view.deleted == []

    clock.now = START + NOTICE_LIFETIME
    await notifier.pass_once()
    assert view.deleted == [100]


async def test_a_failed_rollover_message_is_deleted_once_resolved() -> None:
    view, clock = _View(), _Clock()
    predecessor = _record()
    resolved = False

    async def settled(rollover_id: str) -> bool:
        assert rollover_id == "r1"
        return resolved

    notifier = _rollover_notifier(view, clock, settled)
    await notifier.notify(RolloverReport(_rollover(predecessor), predecessor))
    clock.now = START + timedelta(minutes=5)
    await notifier.pass_once()
    assert view.deleted == []

    resolved = True
    await notifier.pass_once()
    assert view.deleted == [100]


class _Rollovers:
    def __init__(self, *rows: Rollover) -> None:
        self.rows = {row.id: row for row in rows}
        self.latest: dict[SessionId, Rollover] = {}

    async def get(self, rollover_id: str) -> Rollover | None:
        return self.rows.get(rollover_id)

    async def latest_for(self, predecessor: SessionId) -> Rollover | None:
        return self.latest.get(predecessor)


async def test_the_rule_waits_while_the_session_the_failure_kept_is_still_running() -> None:
    predecessor = _record()
    row = _rollover(predecessor)
    rollovers = _Rollovers(row)
    rollovers.latest[predecessor.session_id] = row

    async def session(_: SessionId) -> SessionRecord:
        return predecessor

    assert await rollover_notice_settled(rollovers, session)("r1") is False


async def test_the_rule_ends_the_notice_once_the_session_is_stopped() -> None:
    predecessor = _record(SessionState.ENDED)
    row = _rollover(predecessor)
    rollovers = _Rollovers(row)
    rollovers.latest[predecessor.session_id] = row

    async def session(_: SessionId) -> SessionRecord:
        return predecessor

    assert await rollover_notice_settled(rollovers, session)("r1") is True


async def test_the_rule_ends_the_notice_once_a_newer_rollover_opened() -> None:
    predecessor = _record()
    row = _rollover(predecessor)
    rollovers = _Rollovers(row)
    rollovers.latest[predecessor.session_id] = _rollover(predecessor, id="r2")

    async def session(_: SessionId) -> SessionRecord:
        return predecessor

    assert await rollover_notice_settled(rollovers, session)("r1") is True


async def test_the_rule_leaves_a_gone_predecessor_s_notice_to_time() -> None:
    """The session was already stopped when it was sent; it must not vanish at once."""
    predecessor = _record(SessionState.ENDED)
    row = _rollover(predecessor, code="predecessor-gone")
    rollovers = _Rollovers(row)
    rollovers.latest[predecessor.session_id] = row

    async def session(_: SessionId) -> SessionRecord:
        return predecessor

    assert await rollover_notice_settled(rollovers, session)("r1") is False


# The shared helper --------------------------------------------------------------------


async def test_a_delete_telegram_refuses_is_not_retried_forever() -> None:
    view = _View(refuse_delete=True)
    store = InMemoryExpiringNoticeStore()
    messages = ExpiringMessages(view=view, kind="limit-reset", store=store)
    await store.remember(ExpiringNotice(4242, 7, "limit-reset", "codex", START))

    await messages.sweep(object(), START)

    assert await store.standing("limit-reset") == ()


async def test_a_delete_that_never_reached_telegram_is_tried_again() -> None:
    class _Down(_View):
        async def discard(self, bot: object, message_id: int) -> bool:
            raise OSError("network down")

    store = InMemoryExpiringNoticeStore()
    messages = ExpiringMessages(view=_Down(), kind="limit-reset", store=store)
    await store.remember(ExpiringNotice(4242, 7, "limit-reset", "codex", START))

    await messages.sweep(object(), START)

    assert len(await store.standing("limit-reset")) == 1
