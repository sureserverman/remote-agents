"""Finished limit-stop outcomes are pruned after 90 days, and nothing that matters is (BL-111).

`limit_stop_outcomes` gains one row per limit stop and nothing deleted any. The owner ruled
on 2026-10-08: prune rows older than 90 days. Two kinds of row are never pruned, whatever
their age:

* a `NUDGING` intent -- it is what stops a nudge being typed twice (DEC-099);
* a row whose session can still act -- any state the lifecycle matrix offers a way out of.
  `unresolved` reads a stop as undecided when its outcome row is missing, so pruning the outcome
  of a running session's newest stop would make it undecided again and it could be acted on a
  second time. `failed`, `orphaned` and `preserved` can all return to running, so only
  `TERMINAL_STATES` (today, `ended`) and sessions no longer recorded count as finished.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.limit_stop_store import SQLiteLimitStopStore
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.application.limit_resume import OUTCOME_RETENTION
from remote_agents.domain.models import SessionState
from remote_agents.domain.state_machine import TERMINAL_STATES
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity, LimitHit
from remote_agents.ports.limit_stop_outcomes import LIFTED, NUDGING

_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
_OLD = _NOW - timedelta(days=91)
_RECENT = _NOW - timedelta(days=89)


@pytest.fixture
def connection(tmp_path):
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    yield connection
    connection.close()


def _session(connection, session_id: str, state: str) -> None:
    with connection:
        connection.execute(
            "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, "
            "state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, "p1", "claude", "Demo", state, _OLD.isoformat()),
        )


def _outcome(connection, session_id: str, outcome: str, decided_at: datetime) -> None:
    with connection:
        connection.execute(
            "INSERT INTO limit_stop_outcomes(session_id, stopped_at, outcome, decided_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, decided_at.isoformat(), outcome, decided_at.isoformat()),
        )


def _sessions_with_outcomes(connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT session_id FROM limit_stop_outcomes")}


def test_the_retention_is_ninety_days() -> None:
    assert timedelta(days=90) == OUTCOME_RETENTION


async def test_an_old_outcome_of_a_finished_session_is_pruned_and_counted(connection) -> None:
    for state in sorted(TERMINAL_STATES):
        _session(connection, f"done-{state.value}", state.value)
        _outcome(connection, f"done-{state.value}", LIFTED, _OLD)
    _outcome(connection, "gone-1", LIFTED, _OLD)  # its session row no longer exists

    pruned = await SQLiteLimitStopStore(connection).prune(_NOW - OUTCOME_RETENTION)

    assert pruned == len(TERMINAL_STATES) + 1
    assert _sessions_with_outcomes(connection) == set()


async def test_a_recent_outcome_stays(connection) -> None:
    _session(connection, "ended-1", "ended")
    _outcome(connection, "ended-1", LIFTED, _RECENT)

    assert await SQLiteLimitStopStore(connection).prune(_NOW - OUTCOME_RETENTION) == 0
    assert _sessions_with_outcomes(connection) == {"ended-1"}


async def test_an_old_nudging_intent_is_never_pruned(connection) -> None:
    _session(connection, "ended-1", "ended")
    _outcome(connection, "ended-1", NUDGING, _OLD)

    assert await SQLiteLimitStopStore(connection).prune(_NOW - OUTCOME_RETENTION) == 0
    assert _sessions_with_outcomes(connection) == {"ended-1"}


@pytest.mark.parametrize(
    "state", sorted(state.value for state in SessionState if state not in TERMINAL_STATES)
)
async def test_an_old_outcome_of_a_session_that_can_still_act_stays(connection, state) -> None:
    """Swept over every non-terminal state, so a state added later is covered without an edit."""
    _session(connection, "live-1", state)
    _outcome(connection, "live-1", LIFTED, _OLD)

    assert await SQLiteLimitStopStore(connection).prune(_NOW - OUTCOME_RETENTION) == 0
    assert _sessions_with_outcomes(connection) == {"live-1"}


async def test_pruning_never_makes_a_running_sessions_stop_undecided_again(connection) -> None:
    """The hazard in full: a running session whose newest news is an old, decided stop."""
    session_id = "11111111-1111-4111-8111-111111111111"
    _session(connection, session_id, "running")
    stop = AgentActivity(
        session_id,
        ActivityKind.LIMIT_REACHED,
        None,
        _OLD,
        limit=LimitHit("5h", _OLD + timedelta(hours=2)),
    )
    await SQLiteActivityStore(connection).append(stop)
    store = SQLiteLimitStopStore(connection)
    (undecided,) = await store.unresolved((session_id,))
    assert await store.record(undecided, LIFTED, decided_at=_OLD)

    await store.prune(_NOW - OUTCOME_RETENTION)

    assert await store.unresolved((session_id,)) == ()


async def test_a_prune_that_raises_costs_one_pass_and_never_escapes(caplog) -> None:
    """The service's daily pass logs a failing store and carries on, like every other pass."""
    from types import SimpleNamespace

    from remote_agents.composition.service import _prune_limit_stop_outcomes_once

    class _Refusing:
        async def prune(self, before):
            raise RuntimeError("database is locked")

    await _prune_limit_stop_outcomes_once(SimpleNamespace(limit_stop_outcomes=_Refusing()))

    assert "limit-stop outcome prune could not complete" in caplog.text
