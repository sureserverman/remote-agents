"""The grouping half of the notification policy, now that it lives in the application layer.

These cases were `tests/unit/adapters/telegram/test_notifications.py`'s and moved here with the
functions they exercise; their assertions are relocated, not rewritten. What is new is the pair
of guards at the top -- the purity sweep, and the sweep for the shape the limit rule cannot be
expressed without (DEC-043).
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application import notification_policy
from remote_agents.application.notification_policy import (
    SessionGroup,
    for_update,
    grouped_for_delivery,
    shown_in_message,
)
from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
)

SESSION_A = "0191f2c2-0000-7000-8000-00000000aaaa"
SESSION_B = "0191f2c2-0000-7000-8000-00000000bbbb"
OBSERVED = datetime(2026, 8, 11, 14, 5, tzinfo=UTC)


def _observed(
    session_id: str,
    kind: ActivityKind,
    *,
    detail: str | None = None,
    minute: int = 0,
    confidence: ActivityConfidence = ActivityConfidence.REPORTED,
) -> AgentActivity:
    return AgentActivity(
        session_id=session_id,
        kind=kind,
        detail=detail,
        observed_at=OBSERVED + timedelta(minutes=minute),
        confidence=confidence,
    )


# The two guards ---------------------------------------------------------------------------


def _imported_modules() -> set[str]:
    """Every module name this policy module imports, read from its source rather than run."""
    source = pathlib.Path(notification_policy.__file__).read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_policy_module_imports_no_delivery_machinery() -> None:
    """The point of the move: this is policy, and policy cannot reach the wire.

    Swept over the parsed import graph rather than over a `grep`, so an import written in any
    of the forms `grep` misses -- aliased, relative, inside a `TYPE_CHECKING` block -- is still
    seen. The three names are the delivery stack this module was extracted out of: `telegram`
    is PTB, `httpx` is the transport underneath it.
    """
    forbidden = {"telegram", "httpx"}
    reached = {name.split(".")[0] for name in _imported_modules()}

    assert not (reached & forbidden), f"the policy module reaches delivery machinery: {reached}"


def test_the_policy_module_reads_no_clock() -> None:
    """Every moment this module reasons about arrives as data on an observation.

    A relocation that quietly acquired a clock would be untestable in exactly the way the
    grouping rules below are testable: they are handed everything a pass observed and read
    nothing else, which is why none of these cases needs a fake clock or a sleep.
    """
    source = pathlib.Path(notification_policy.__file__).read_text(encoding="utf-8")
    clock_calls = [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "utcnow", "today", "monotonic", "time"}
    ]

    assert clock_calls == [], f"the policy module reads a clock: {clock_calls}"


@pytest.mark.parametrize("function", [shown_in_message, for_update])
def test_the_line_limit_is_asked_for_and_never_assumed(function: object) -> None:
    """The number belongs to the surface; only the rule for spending it moved (DEC-034 cost 4).

    Swept for the **shape** the rule cannot be expressed without rather than for the constant's
    name (DEC-043): a default here -- any default, however faithful to the bot's five -- is the
    function acquiring an opinion about a quantity a second frontend would answer differently,
    and it would acquire it silently, because every existing caller passes the value anyway.
    Keyword-only so a positional call cannot drift onto it either.
    """
    limit = inspect.signature(function).parameters.get("limit")

    assert limit is not None, "the limit must be asked for"
    assert limit.kind is inspect.Parameter.KEYWORD_ONLY, "the limit must be keyword-only"
    assert limit.default is inspect.Parameter.empty, "the limit must have no default"


# Grouping a pass's observations -- relocated from the adapter's suite ------------------------


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


def test_the_same_kind_said_twice_is_one_sentence_carrying_the_newer_words() -> None:
    """The owner's rule: just the last of them.

    Keyed on `(kind, detail)` these were two distinct things and both were rendered, which is
    how one session's message became a wall of "the agent has finished its work" with five
    different last replies hanging off it. The sentence is what the notification says, and it
    says the session stopped and wants them -- once, in whatever words are current.
    """
    groups = grouped_for_delivery(
        [
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="Ran the suite.", minute=1),
            _observed(SESSION_A, ActivityKind.COMPLETED, detail="Pushed the branch.", minute=2),
        ]
    )

    assert len(groups) == 1
    assert [activity.detail for activity in groups[0].activities] == ["Pushed the branch."]


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
