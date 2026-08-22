"""The grouping half of the notification policy, now that it lives in the application layer.

These cases were `tests/unit/adapters/telegram/test_notifications.py`'s and moved here with the
functions they exercise; their bodies and the `_observed` fixture that feeds them are relocated,
not rewritten. What is new is the guards at the top -- the purity sweep, the clock sweep, and the
sweep for the shape the limit rule cannot be expressed without (DEC-043).

`_observed` derives `confidence` from the kind rather than taking it, and that is the fixture's
one load-bearing detail: `QUIET` is the project's only inferred observation, and a flat
`REPORTED` default here would stamp it wrongly. It is written this way because the Stage 1 gate's
Tier-2 review found it written the other way -- the fixture had been rewritten while the move was
being described as pure, and the cases could not see it, because they build *both* sides of every
comparison with this helper and so drift together (DEC-019's recorded failure mode).
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from collections import deque
from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application import notification_policy
from remote_agents.application.notification_policy import (
    Sent,
    SessionGroup,
    due,
    enqueue,
    for_update,
    forget_expired,
    grouped_for_delivery,
    record_sent,
    shown_in_message,
    window,
)
from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
)

SESSION_A = "0191f2c2-0000-7000-8000-00000000aaaa"
SESSION_B = "0191f2c2-0000-7000-8000-00000000bbbb"
OBSERVED = datetime(2026, 8, 11, 14, 5, tzinfo=UTC)
EVERY_KIND_IN_ORDER = (
    ActivityKind.COMPLETED,
    ActivityKind.LIMIT_REACHED,
    ActivityKind.OUTPUT_LIMIT,
    ActivityKind.NEEDS_ANSWER,
)


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


# The guards ---------------------------------------------------------------------------


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

    Matches a bare name as well as an attribute, because `from time import monotonic` then
    `monotonic()` parses as neither an attribute nor anything `datetime.now()` would catch --
    the close-out evaluator found that hole.

    **Disclosed limit.** A clock reached through an alias this list does not name
    (`from time import monotonic as tick`) still slips past. Naming the readings is the only
    sweep available short of forbidding every zero-argument call, and that would forbid most of
    the module.
    """
    source = pathlib.Path(notification_policy.__file__).read_text(encoding="utf-8")
    reading = {"now", "utcnow", "today", "monotonic", "perf_counter", "time"}
    clock_calls = [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "attr", None) in reading or getattr(node.func, "id", None) in reading
        )
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


# The suppression window: its taper, and what resets it -------------------------------------


def _sent_map() -> dict[tuple[str, ActivityKind], Sent]:
    """The suppression map, which stays with the surface; only the rules for it moved.

    Handed to every function below rather than owned by one, which is what "the moved policy is
    pure" means here: there is no policy object holding state between calls, so a case can put
    the map into any shape it likes and ask one question about it.
    """
    return {}


def test_the_window_doubles_per_repeat_to_a_cap_of_five_doublings() -> None:
    """DEC-013 clause (5), walked to its cap: 2 minutes, 4, 8, 16, 32, then 64 for ever.

    Walked rather than sampled. A test that checks one doubling passes against a taper that
    doubles once and then stops, and against one that never caps and reaches nineteen hours by
    the tenth repeat -- and DEC-031 records that the failure here is invisible in a unit test
    and only shows up over hours of real delivery, which is exactly why the whole curve is
    asserted rather than a point on it.
    """
    rate_limit = timedelta(seconds=120)

    curve = [window(repeats, rate_limit=rate_limit) for repeats in range(8)]

    assert curve == [
        timedelta(minutes=2),
        timedelta(minutes=4),
        timedelta(minutes=8),
        timedelta(minutes=16),
        timedelta(minutes=32),
        timedelta(minutes=64),
        timedelta(minutes=64),
        timedelta(minutes=64),
    ], "the taper is 2/4/8/16/32/64 and then flat -- not unbounded, not stalled early"


def test_a_repeat_of_the_same_kind_climbs_the_taper() -> None:
    """The count is what makes each wait longer, so it has to survive being read."""
    sent = _sent_map()
    moment = OBSERVED

    for _ in range(3):
        record_sent(sent, SESSION_A, [ActivityKind.COMPLETED], moment)

    assert sent[(SESSION_A, ActivityKind.COMPLETED)].repeats == 2


def test_a_different_kind_resets_that_sessions_repeat_counts() -> None:
    """DEC-013 clause (5): a repeat count claims nothing has changed, and a new kind is evidence.

    The reset lands on the kinds the session did *not* say this pass. The kinds the message
    carried are exempt, because recording the second of them would otherwise reset the first,
    which was stamped a moment earlier in the same send.
    """
    sent = _sent_map()
    for _ in range(4):
        record_sent(sent, SESSION_A, [ActivityKind.COMPLETED], OBSERVED)
    assert sent[(SESSION_A, ActivityKind.COMPLETED)].repeats == 3

    record_sent(sent, SESSION_A, [ActivityKind.NEEDS_ANSWER], OBSERVED)

    assert sent[(SESSION_A, ActivityKind.COMPLETED)].repeats == 0, (
        "a different kind is evidence something changed, so the taper starts over"
    )
    assert sent[(SESSION_A, ActivityKind.COMPLETED)].sent_at == OBSERVED, (
        "only the counter resets; the stamp stays, so the base window still collapses a burst"
    )


def test_two_kinds_in_one_message_do_not_reset_each_other() -> None:
    """The exemption, which is the whole reason `record_sent` takes a batch and not a key.

    Applied per-kind to a grouped message the rule turns on itself. DEC-031 measured what that
    costs: a standing condition backed off to sixty-four minutes is absent from almost every
    message, so it was almost always the kind being reset -- by its own suppression.
    """
    sent = _sent_map()
    for _ in range(3):
        record_sent(sent, SESSION_A, [ActivityKind.COMPLETED, ActivityKind.NEEDS_ANSWER], OBSERVED)

    assert sent[(SESSION_A, ActivityKind.COMPLETED)].repeats == 2
    assert sent[(SESSION_A, ActivityKind.NEEDS_ANSWER)].repeats == 2


def test_another_sessions_counts_are_untouched_by_a_reset() -> None:
    """The reset is scoped to the session that spoke, because a repeat count is about it."""
    sent = _sent_map()
    for _ in range(3):
        record_sent(sent, SESSION_A, [ActivityKind.COMPLETED], OBSERVED)
        record_sent(sent, SESSION_B, [ActivityKind.COMPLETED], OBSERVED)

    record_sent(sent, SESSION_A, [ActivityKind.NEEDS_ANSWER], OBSERVED)

    assert sent[(SESSION_A, ActivityKind.COMPLETED)].repeats == 0
    assert sent[(SESSION_B, ActivityKind.COMPLETED)].repeats == 2, "B said nothing new"


def test_news_is_due_when_its_own_window_has_passed_and_not_before() -> None:
    """`due` reads the window the entry's own count earns, not the base one."""
    sent = _sent_map()
    rate_limit = timedelta(seconds=120)
    activity = _observed(SESSION_A, ActivityKind.COMPLETED, detail="ran it")

    assert due(activity, sent, OBSERVED, rate_limit=rate_limit), "nothing sent yet is always news"

    for _ in range(3):
        record_sent(sent, SESSION_A, [ActivityKind.COMPLETED], OBSERVED)

    assert not due(activity, sent, OBSERVED + timedelta(minutes=7), rate_limit=rate_limit), (
        "at two repeats the window is eight minutes, and seven is inside it"
    )
    assert due(activity, sent, OBSERVED + timedelta(minutes=8), rate_limit=rate_limit)


# Retention: how long a lapsed entry's repeat count is kept ---------------------------------


def test_an_entry_at_zero_repeats_is_kept_for_the_floor_not_for_its_own_window() -> None:
    """DEC-031's carried correction, asserted at the exact point the old rule broke.

    Under a horizon of `window(repeats) * 2` a fresh entry lived four minutes. This asserts the
    floor instead: at zero repeats the entry is still held well past that, because the horizon
    does not consult the count at all.

    The bootstrapping problem is why a proportional horizon cannot fix itself -- the entry must
    already have a high count to be kept long enough to earn a high count -- and it is why this
    case is written at `repeats=0` rather than anywhere more comfortable.
    """
    rate_limit = timedelta(seconds=120)
    sent = {(SESSION_A, ActivityKind.COMPLETED): Sent(OBSERVED, 0)}

    forget_expired(sent, OBSERVED + timedelta(minutes=5), rate_limit=rate_limit)

    assert sent, "a fresh entry was discarded on a horizon computed from its own zero count"


def test_a_kind_reporting_slower_than_its_first_window_still_reaches_the_taper() -> None:
    """DEC-031's measurement, run: 12 messages over eight hours, not 96.

    A `Stop` fires per turn and a turn routinely takes longer than four minutes, so a kind
    reporting every five is the *ordinary* case rather than a corner. Under the old horizon its
    entry was always already discarded, was re-created at zero, and so could never reach the
    first doubling -- 96 notifications over eight hours, every one of them individually true.

    Simulated end to end rather than asserted step by step, because the defect is a property of
    the whole run: every individual step was correct, and only the total was wrong.
    """
    rate_limit = timedelta(seconds=120)
    sent: dict[tuple[str, ActivityKind], Sent] = {}
    activity = _observed(SESSION_A, ActivityKind.COMPLETED, detail="finished a turn")
    delivered = 0

    for step in range(0, 8 * 60, 5):
        moment = OBSERVED + timedelta(minutes=step)
        forget_expired(sent, moment, rate_limit=rate_limit)
        if due(activity, sent, moment, rate_limit=rate_limit):
            record_sent(sent, SESSION_A, [ActivityKind.COMPLETED], moment)
            delivered += 1

    assert delivered == 12, (
        f"the taper delivered {delivered} messages over eight hours; it intends 12, "
        "and the defect DEC-031 records produced 96"
    )


def test_a_kind_that_genuinely_stopped_reporting_is_eventually_forgotten() -> None:
    """The floor is a floor, not a removal: the map is still bounded.

    One entry per (session, kind) is small and unbounded over the life of a service launching
    sessions all day, so forgetting is what bounds it. The whole price of the floor is that a
    silent kind is forgotten an hour or so later than it used to be.
    """
    rate_limit = timedelta(seconds=120)
    sent = {(SESSION_A, ActivityKind.COMPLETED): Sent(OBSERVED, 0)}

    forget_expired(sent, OBSERVED + timedelta(hours=3), rate_limit=rate_limit)

    assert sent == {}, "a session that stopped reporting must not be kept for ever"


# The bounded backlog: what a full queue costs, and whom ------------------------------------


def test_the_backlog_holds_up_to_its_cap_without_evicting_anything() -> None:
    """Nothing is dropped until the cap is actually reached."""
    held: deque[AgentActivity] = deque()

    reports = enqueue(
        held,
        [_observed(SESSION_A, ActivityKind.COMPLETED, minute=n) for n in range(5)],
        maximum=5,
    )

    assert reports == ()
    assert len(held) == 5


def test_the_cap_costs_the_session_that_filled_it() -> None:
    """DEC-031: eviction falls on the session using the most of the queue, not on its head.

    Delivery is per session and so is fairness -- grouping orders by first appearance precisely
    so a burst cannot starve a quiet session -- but retention used to be global and per
    observation. Simulated then: five sessions reporting `QUIET` once against one session
    emitting twenty-five records a pass ended with the queue holding only the loud session, and
    the quiet reports were destroyed permanently, because `observe_quiet` fires once per spell
    and the drain had already deleted the files.
    """
    held: deque[AgentActivity] = deque()
    enqueue(held, [_observed(SESSION_B, ActivityKind.QUIET, minute=1)], maximum=4)
    enqueue(
        held,
        [_observed(SESSION_A, ActivityKind.COMPLETED, minute=n) for n in range(2, 5)],
        maximum=4,
    )

    reports = enqueue(held, [_observed(SESSION_A, ActivityKind.NEEDS_ANSWER, minute=9)], maximum=4)

    assert reports == ((SESSION_A, 3),), "the loudest session pays, and the report says who"
    assert SESSION_B in {activity.session_id for activity in held}, (
        "the quiet session's only report was evicted by a louder neighbour"
    )


def test_the_evicted_observation_is_the_loudest_sessions_oldest() -> None:
    """Within one session the newest news is the news worth keeping."""
    held: deque[AgentActivity] = deque()
    enqueue(
        held,
        [_observed(SESSION_A, kind, minute=n) for n, kind in enumerate(EVERY_KIND_IN_ORDER)],
        maximum=len(EVERY_KIND_IN_ORDER),
    )
    oldest = held[0]

    enqueue(held, [_observed(SESSION_A, ActivityKind.QUIET, minute=99)], maximum=len(held))

    assert oldest not in held
    assert len(held) == len(EVERY_KIND_IN_ORDER)


def test_eviction_reports_rather_than_says() -> None:
    """DEC-043: the policy hands back a signal; the sentence in the journal is the surface's.

    `enqueue` returns `(session_id, how_many_it_held)` per eviction and writes no message. The
    warning the operator reads is `ActivityNotifier`'s, because the wording is sized for a
    journal line and a second frontend would size it differently -- or, having no journal of
    its own, would not write one at all.
    """
    held: deque[AgentActivity] = deque()
    enqueue(
        held, [_observed(SESSION_A, ActivityKind.COMPLETED, minute=n) for n in range(3)], maximum=3
    )

    reports = enqueue(held, [_observed(SESSION_A, ActivityKind.QUIET, minute=9)], maximum=3)

    assert reports == ((SESSION_A, 3),)
    session_id, count = reports[0]
    assert " " not in session_id, "a session id, not a sentence about one"
    assert isinstance(count, int), "a count the surface may word however it likes"
