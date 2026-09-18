"""The early-reset detector, as a table of the cases that must stay silent.

The table is mostly negatives, and that is the point rather than an accident of writing. The
message this rule fires costs the owner a phone buzz asserting something they cannot verify
afterwards -- the window that reset is, by then, a window that has reset -- so a false positive
is not a cosmetic defect, it is the service lying about the one figure the owner keeps this
service for. Every guard below therefore fails towards silence, and each negative case names
the specific real reading that would otherwise have been read as news: a scheduled rollover, a
source the owner flipped in Settings, a failed read, a reading that arrived out of order.

`detect` is asked for by `LimitResetNotifier` and by nothing else today. It is tested here
rather than through that notifier because the rule is the part with cases in it: threading
eleven of them through a fake sender and a fake clock would be eleven tests of the fake
(DEC-043's division -- this module decides, the surface words it).
"""

from __future__ import annotations

import ast
import pathlib
import sys
from datetime import UTC, datetime, timedelta, timezone

import pytest

from remote_agents.application import limit_resets
from remote_agents.application.limit_resets import (
    EARLY_RESET_GRACE,
    MINIMUM_DROP_POINTS,
    EarlyReset,
    detect,
)
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsageWindow

NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
CLAUDE = ProfileId("claude")
AHEAD = NOW + timedelta(hours=3)
"""A reset instant comfortably ahead of `NOW` -- the shape every positive case needs."""


def _reading(
    *windows: UsageWindow,
    minute: int = 0,
    source: str | None = "status line",
    absence: LimitsAbsence | None = None,
) -> AgentLimits:
    """One provider's limits reading, stamped by default the way Claude's hop stamps its own.

    `minute` moves `observed_at`, which every comparison needs to advance and which no case
    below should have to spell twice. The default source is a stamp rather than `None` because
    the readings this rule actually compares are stamped ones: Claude's come from the
    status-line hop and Codex's from the rollout when its app server could not answer
    (DEC-061, DEC-087). A case that cares about the stamp passes its own.
    """
    return AgentLimits(
        CLAUDE,
        windows,
        observed_at=NOW + timedelta(minutes=minute),
        stale_source=source,
        absence=absence,
    )


def _window(
    label: str, percent: float, *, resets_in: timedelta | None = timedelta(hours=3)
) -> UsageWindow:
    """One window, by default one whose recorded reset is still well ahead of `NOW`."""
    return UsageWindow(label, percent, None if resets_in is None else NOW + resets_in)


# The guard on the module itself -------------------------------------------------------


def _imported_modules() -> set[str]:
    """Every module name the detector imports, read from its source rather than run."""
    source = pathlib.Path(limit_resets.__file__).read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_detector_imports_only_ports_and_the_standard_library() -> None:
    """The rule may reach the types it reasons about, and nothing that can fetch them.

    Swept over the parsed import graph rather than a `grep`, so an aliased, relative or
    `TYPE_CHECKING` import is still seen -- the form a `grep` misses is exactly the form a
    later edit reaches for. A reader imported here would let this module choose *where* the
    figures come from, which is the one thing it must not do: the source is the owner's
    setting (DEC-087) and the notifier's `Backend.limits` is the only way in.

    `remote_agents.domain` would be legal under ARCH-02 and is swept out anyway, because the
    detector has no reason to name a domain type and a future import of one would most likely
    be a session or a project sneaking into an account-level rule.
    """
    internal = {name for name in _imported_modules() if name.startswith("remote_agents")}
    external = {
        name.split(".")[0] for name in _imported_modules() if not name.startswith("remote_agents")
    }

    assert internal <= {"remote_agents.ports.agent_usage"}, (
        f"the detector reaches inward: {internal}"
    )
    assert external <= set(sys.stdlib_module_names), f"the detector reaches outward: {external}"


def test_the_two_constants_are_module_level_names() -> None:
    """Not config keys, and not literals buried in the comparison (design, 2026-09-18).

    No owner has asked to tune either, so neither becomes a schema field; but a reader
    checking whether a reported reset was really early has to be able to find the two numbers
    without reading the arithmetic.
    """
    assert EARLY_RESET_GRACE == timedelta(minutes=5)
    assert MINIMUM_DROP_POINTS == 10


# What counts as news ------------------------------------------------------------------


def test_one_window_dropping_while_its_reset_was_still_ahead_is_reported() -> None:
    """The whole feature, in its smallest form: 91% to 2% with three hours still to run."""
    results = detect(
        _reading(_window("5h", 91.0)),
        _reading(_window("5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == (
        EarlyReset(label="5h", previous_percent=91.0, current_percent=2.0, expected_reset_at=AHEAD),
    )


def test_two_windows_of_one_provider_come_back_in_one_result() -> None:
    """One provider, one detection, one message -- so both windows arrive together.

    The order is the provider's own, which is the order the limits block already renders and
    the order the sentence will read in (`5h 91% -> 2%, week 64% -> 0%`). Ordering here rather
    than leaving it to the presenter keeps a retry from reshuffling a message the owner has
    already read.
    """
    results = detect(
        _reading(_window("5h", 91.0), _window("week", 64.0)),
        _reading(_window("5h", 2.0), _window("week", 0.0), minute=5),
        now=NOW,
    )

    rendered = [
        (result.label, result.previous_percent, result.current_percent) for result in results
    ]

    assert rendered == [("5h", 91.0, 2.0), ("week", 64.0, 0.0)]


def test_a_window_whose_drop_is_exactly_the_threshold_is_reported() -> None:
    """Ten points is news; the boundary is stated by a case rather than by the constant."""
    results = detect(
        _reading(_window("5h", 30.0)),
        _reading(_window("5h", 20.0), minute=5),
        now=NOW,
    )

    assert [result.label for result in results] == ["5h"]


# What stays silent --------------------------------------------------------------------


def test_a_scheduled_rollover_is_not_news() -> None:
    """The window this reading counted against had already ended, so the drop was due.

    This is the case the whole rule exists to exclude: a plan's window rolls over on its own
    several times a week, and a service that reported those would be a service the owner
    silences.
    """
    results = detect(
        _reading(_window("5h", 91.0, resets_in=timedelta(minutes=-1))),
        _reading(_window("5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == ()


def test_a_reset_inside_the_grace_is_not_news() -> None:
    """A rollover the provider records a few minutes late still reads as a rollover.

    `resets_at` is written by the provider and observed by this service on a timer; the two
    clocks are not the same clock, and a scheduled reset landing four minutes after the
    recorded instant is the ordinary case rather than an early wipe.
    """
    results = detect(
        _reading(_window("5h", 91.0, resets_in=timedelta(minutes=4))),
        _reading(_window("5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == ()


def test_the_grace_boundary_itself_is_not_news() -> None:
    """Exactly five minutes ahead is inside the grace, not past it.

    Stated because the comparison is strict and a later reader is entitled to know which side
    of it the boundary falls on without re-deriving it from the source.
    """
    results = detect(
        _reading(_window("5h", 91.0, resets_in=EARLY_RESET_GRACE)),
        _reading(_window("5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == ()


def test_a_drop_one_point_short_of_the_threshold_is_not_news() -> None:
    """Nine points is movement, not a wipe.

    Both providers publish integers that have been seen to wobble by a point or two between
    readings of the same window; a threshold below that would report rounding.
    """
    results = detect(
        _reading(_window("5h", 30.0)),
        _reading(_window("5h", 21.0), minute=5),
        now=NOW,
    )

    assert results == ()


def test_a_window_with_no_recorded_reset_is_not_news() -> None:
    """Without a `resets_at` there is no claim that the drop came early, only that it dropped.

    `UsageWindow.resets_at` is optional because a provider may publish a percentage and no
    instant, and DEC-061's rule is that an absent figure is never replaced by a derived one --
    so a window this service cannot date cannot be called early.
    """
    results = detect(
        _reading(_window("5h", 91.0, resets_in=None)),
        _reading(_window("5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == ()


def test_readings_from_different_sources_are_not_compared() -> None:
    """A source change is not a reset (DEC-061, DEC-087).

    The owner can flip Claude's limits source in Settings, and the reader falls back to the
    rollout when Codex's app server does not answer. Either way the next reading is a
    different provider's arithmetic about a different window, and the difference between two
    of them is not a fact about the owner's plan.
    """
    results = detect(
        _reading(_window("5h", 91.0), source="status line"),
        _reading(_window("5h", 2.0), minute=5, source="usage API"),
        now=NOW,
    )

    assert results == ()


def test_an_unstamped_reading_is_not_compared_with_a_stamped_one() -> None:
    """`None` is a stamp like any other here: it names a different provenance, so it is one.

    The same window read once from the provider's own accounting and once from a borrowed
    file is two measurements with two meanings, and DEC-061 exists to keep them from being
    rendered as though they were one.
    """
    results = detect(
        _reading(_window("5h", 91.0), source=None),
        _reading(_window("5h", 2.0), minute=5, source="status line"),
        now=NOW,
    )

    assert results == ()


@pytest.mark.parametrize("side", ["previous", "current"])
def test_a_no_reading_absence_on_either_side_is_not_compared(side: str) -> None:
    """`NO_READING` is never a baseline and never a conclusion (DEC-061).

    A read that found nothing says so in a type rather than by handing back an empty tuple,
    and the whole reason that type exists is that a silence must not be usable as a figure.
    Read as a figure it would be 0%, which against any live window is a wipe -- so this
    absence, unguarded, is the one that manufactures a false alarm from a missing file.
    """
    absent = _reading(minute=5 if side == "current" else 0, absence=LimitsAbsence.NO_READING)
    present_previous = _reading(_window("5h", 91.0))
    present_current = _reading(_window("5h", 2.0), minute=5)

    results = detect(
        absent if side == "previous" else present_previous,
        absent if side == "current" else present_current,
        now=NOW,
    )

    assert results == ()


@pytest.mark.parametrize("side", ["previous", "current"])
def test_a_reading_that_declares_an_absence_is_refused_even_carrying_figures(side: str) -> None:
    """The guard is on the declaration, not on the emptiness that usually comes with it.

    Written this way because the case above cannot see the guard at all: an absent reading
    carries no windows, so it pairs with nothing and would stay silent with the check deleted.
    Measured -- deleting it left all twenty-six cases green, which is a guard with no caller.

    `AgentLimits` states that windows and an absence are exclusive and deliberately does not
    validate it, because the honest failure is a reader that forgot to say which silence it
    meant and a raise there would take out a whole pane. This is the other end of that choice:
    a reading that says both things is a reader this service does not understand, and a
    percentage from a reader that has already contradicted itself is not evidence of anything.
    """
    contradictory = AgentLimits(
        CLAUDE,
        (_window("5h", 2.0 if side == "current" else 91.0),),
        observed_at=NOW + timedelta(minutes=5 if side == "current" else 0),
        stale_source="status line",
        absence=LimitsAbsence.NO_READING,
    )

    results = detect(
        contradictory if side == "previous" else _reading(_window("5h", 91.0)),
        contradictory if side == "current" else _reading(_window("5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == ()


@pytest.mark.parametrize("minute", [0, -5])
def test_a_reading_that_did_not_advance_is_not_compared(minute: int) -> None:
    """The current reading must be the later observation, or there is no "dropped" to speak of.

    Both stamps say when the *provider* recorded its figures, and a file that has not been
    rewritten carries the same stamp however often it is read. A pair that did not advance is
    therefore either the same observation twice or a late arrival of an older one, and the
    second is the dangerous one: an older reading of a window that has since filled up looks
    exactly like a wipe.
    """
    results = detect(
        _reading(_window("5h", 91.0)),
        _reading(_window("5h", 2.0), minute=minute),
        now=NOW,
    )

    assert results == ()


@pytest.mark.parametrize("side", ["previous", "current"])
def test_an_undated_reading_is_not_compared(side: str) -> None:
    """Without both stamps the order of the two readings is unknown, and unknown is silent.

    The accepted cost, stated here rather than only in the module: a provider that publishes
    figures without saying when it recorded them gets no early-reset report at all. Both
    providers that publish limits do stamp them, and the alternative -- assuming the reading
    in hand is the newer one -- is exactly the assumption the case above shows manufactures a
    wipe out of a late arrival.
    """
    undated = AgentLimits(
        CLAUDE,
        (_window("5h", 91.0 if side == "previous" else 2.0),),
        observed_at=None,
        stale_source="status line",
    )

    results = detect(
        undated if side == "previous" else _reading(_window("5h", 91.0)),
        undated if side == "current" else _reading(_window("5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == ()


@pytest.mark.parametrize("side", ["previous", "current"])
def test_a_label_present_on_only_one_side_is_not_compared(side: str) -> None:
    """A window that appeared or vanished between readings is a plan change, not a reset.

    The labels are the provider's own words for its own windows, and the set of them changes
    when the owner's plan does. There is no previous percentage to have dropped from.
    """
    results = detect(
        _reading(_window("5h" if side == "previous" else "week", 91.0)),
        _reading(_window("week" if side == "previous" else "5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == ()


def test_a_label_repeated_within_one_reading_is_not_compared() -> None:
    """Two windows sharing a label cannot be paired, so neither of them is.

    Neither provider publishes a duplicate today. If one starts, the pairing this rule does
    becomes a guess about which of two rows the earlier one was, and a guess is what the
    whole module is arranged to avoid.
    """
    results = detect(
        _reading(_window("5h", 91.0), _window("5h", 90.0)),
        _reading(_window("5h", 2.0), minute=5),
        now=NOW,
    )

    assert results == ()


def test_two_providers_readings_are_never_compared() -> None:
    """`detect` is asked about one provider; handed two, it answers about neither.

    The caller loops over providers and could pair them wrongly in exactly one way -- a
    dictionary keyed on something that is not the profile. Cheap to refuse here, and the
    refusal is silence rather than a raise, like every other guard.
    """
    results = detect(
        _reading(_window("5h", 91.0)),
        AgentLimits(
            ProfileId("codex"),
            (_window("5h", 2.0),),
            observed_at=NOW + timedelta(minutes=5),
            stale_source="status line",
        ),
        now=NOW,
    )

    assert results == ()


def test_the_first_reading_after_a_start_reports_nothing() -> None:
    """No baseline, no comparison -- the service's first tick is a baseline and nothing else.

    The accepted cost of holding the baseline in memory (design, 2026-09-18): an early reset
    in the minutes around a restart is missed. Missed, never invented.
    """
    assert detect(None, _reading(_window("5h", 2.0), minute=5), now=NOW) == ()


# Totality -----------------------------------------------------------------------------


def test_mixed_awareness_stamps_are_silent_rather_than_fatal() -> None:
    """A naive `resets_at` beside an aware `now` raises `TypeError` if compared carelessly.

    Not hypothetical: the two instants come from different places -- one parsed out of a
    provider's file, one taken from the service's clock -- and a reader that ever emits a
    naive datetime would otherwise take down the loop that calls this, on a timer, for a
    notification nobody needs.
    """
    naive = AgentLimits(
        CLAUDE,
        (UsageWindow("5h", 91.0, datetime(2026, 9, 18, 17, 0)),),
        observed_at=datetime(2026, 9, 18, 14, 0),
        stale_source="status line",
    )

    assert detect(naive, _reading(_window("5h", 2.0), minute=5), now=NOW) == ()


def test_an_offset_aware_stamp_in_another_zone_is_compared_normally() -> None:
    """Awareness is what the comparison needs, not agreement about which zone to say it in."""
    elsewhere = timezone(timedelta(hours=2))
    previous = AgentLimits(
        CLAUDE,
        (UsageWindow("5h", 91.0, AHEAD.astimezone(elsewhere)),),
        observed_at=NOW.astimezone(elsewhere),
        stale_source="status line",
    )

    results = detect(previous, _reading(_window("5h", 2.0), minute=5), now=NOW)

    assert [result.label for result in results] == ["5h"]


def test_the_detector_never_raises_over_the_whole_table() -> None:
    """Totality, swept once over every pairing the cases above build, plus the empty ones.

    The loop calling this is a periodic one beside reconcile, activity and trust, and a
    notification rule that can raise takes the loop's other work with it. Every guard above
    returns `()`; this asserts that no *pairing* of the readings they use finds a path that
    raises instead -- including the pairings no case names, which is where a guard added later
    will first go wrong.
    """
    readings: list[AgentLimits | None] = [
        None,
        _reading(),
        _reading(absence=LimitsAbsence.NO_READING),
        _reading(absence=LimitsAbsence.UNREADABLE),
        _reading(absence=LimitsAbsence.NOT_REPORTED),
        _reading(_window("5h", 91.0)),
        _reading(_window("5h", 91.0, resets_in=None)),
        _reading(_window("5h", 0.0), minute=5),
        _reading(_window("5h", 100.0), minute=-5, source=None),
        _reading(_window("5h", 2.0), _window("week", 0.0), minute=5),
        AgentLimits(CLAUDE, (UsageWindow("5h", 91.0, datetime(2026, 9, 18, 17, 0)),)),
        AgentLimits(ProfileId("codex"), (_window("week", 50.0),), observed_at=NOW),
    ]

    for previous in readings:
        for current in readings:
            results = detect(previous, current, now=NOW)
            assert isinstance(results, tuple)
            assert all(isinstance(result, EarlyReset) for result in results)
