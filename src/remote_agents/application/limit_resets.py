"""When a provider wiped a plan's usage meters ahead of the schedule it had published.

Anthropic and OpenAI occasionally clear a plan's windows early. The owner currently finds out
by accident -- by opening a session, seeing 2% where 91% stood an hour ago, and inferring it --
and the whole of this module is the inference, written down once so that a periodic loop can
make it instead.

**The rule lives here and is asked, never restated (DEC-043, DEC-007).** The notifier that
sends the message and the loop that drives it are both forbidden from re-deciding any part of
it: they hand over two readings and a moment and receive a verdict. That division is not
tidiness. A reset is reported once, to a phone, about a figure that can no longer be checked
afterwards -- the window that was wiped is, by the time the owner looks, simply a window that
has been wiped -- so a second copy of the rule that drifted by a point or a minute would
produce a message nobody could ever disprove.

**Every guard fails towards silence, and the reason is the same asymmetry.** A missed early
reset costs the owner nothing they had before; an invented one costs them their trust in every
limits figure this service shows. So a reading that cannot be compared is not compared: a
different source stamp, a reading that did not advance, an undated one, an absence, a window
that appeared or vanished, a duplicate label. Each of those is a real reading this project has
seen or can expect, not a hypothetical.

**Source stamps are never crossed (DEC-061, DEC-087).** Claude's limits come from the
status-line hop's recording or, opt-in, from the usage API, and Codex's from its app server or
from the rollout file when the app server could not answer. Each reader stamps `stale_source`
with where it answered from. Two readings carrying different stamps are two different
measurements of two differently-derived numbers, and their difference is a fact about this
project's plumbing rather than about the owner's plan. `LimitsAbsence.NO_READING` is the same
rule at its sharpest: read as a figure an absence is 0%, and 0% against any live window is a
wipe, so the one silence that would manufacture a false alarm out of a missing file is the
silence that must never become a baseline.

**Nothing here reads a clock, a file or a provider.** `now` arrives as an argument, which is
what lets the eleven cases in `tests/unit/application/test_limit_resets.py` be a table rather
than a fake clock threaded through a fake sender. It is also what keeps this module unable to
choose a source: it cannot reach a reader, so the owner's Settings choice stays the only thing
that decides where a figure came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

#: How far ahead of its published instant a wipe must be observed to count as early.
#:
#: **Not a guard against scheduled rollovers, which are silent without it.** A rollover is
#: observed *after* the instant it was published for, so `resets_at > now` is already false by
#: the time the drop is visible and the rule refuses it for any grace at all, including zero.
#: An earlier version of this comment claimed the grace had to exceed the polling period or
#: rollovers would be reported on a schedule; that is not what the arithmetic does, the two
#: values are equal anyway, and the comment beside `_LIMITS_POLL_SECONDS` claimed the opposite
#: ordering of the same pair. Found by a Task 2.6 reader, 2026-09-18.
#:
#: What it actually buys is the boundary. `resets_at` is written against the provider's clock
#: and read against ours, so a window observed a minute before its own deadline may simply be
#: a clock disagreeing rather than a wipe -- and a reset that beat its schedule by two minutes
#: is not news anybody can act on. Five minutes covers both and costs only the early resets
#: that were barely early.
#:
#: A module-level name and not a config key: no owner has asked to tune it (design,
#: 2026-09-18), and a knob nobody turns is a schema field, a migration and a settings row that
#: all have to keep agreeing about a number with one correct value.
EARLY_RESET_GRACE = timedelta(minutes=5)

#: How far a window must fall before the fall is a wipe rather than movement.
#:
#: Both providers publish integers, and the same window read twice has been seen to differ by a
#: point or two without anything having happened to the plan. A real early reset is a drop to
#: near zero, so ten points is far below anything this rule needs to catch and far above the
#: noise it must not report. Points rather than a ratio, because `UsageWindow.used_percent` is
#: already a percentage and a second convention here would be one more thing to get backwards.
MINIMUM_DROP_POINTS = 10


@dataclass(frozen=True, slots=True)
class EarlyReset:
    """One window that was wiped while its own recorded reset instant was still ahead.

    A signal and not a sentence (DEC-043): the label is the provider's own word for its own
    window, the two percentages are the figures as the providers published them, and how any
    of it is worded belongs to the surface that says it. The presenter needs exactly the label
    and the pair of percentages to render `5h 91% -> 2%`; they are here because deriving them
    again from the readings would be the notifier holding a second opinion about which window
    reset, which is the thing this module exists to prevent.

    `expected_reset_at` is carried for the journal rather than for the message. When the owner
    asks afterwards why they were told -- and this is a claim they cannot check afterwards --
    the answer is "the provider itself said this window ran until 17:00", and that instant is
    otherwise unreachable without the notifier reading `resets_at` for itself.

    **No `profile_id`.** `detect` is asked about one provider by a caller that named it, so the
    identity is already in the caller's hand -- the same argument `AgentUsage` makes for not
    carrying one where `AgentLimits`, which is obtained without naming a session, must.
    """

    label: str
    previous_percent: float
    current_percent: float
    expected_reset_at: datetime


def detect(
    previous: AgentLimits | None,
    current: AgentLimits | None,
    *,
    now: datetime,
) -> tuple[EarlyReset, ...]:
    """Which of one provider's windows were wiped early between two readings of it.

    Total by construction: every input this cannot make sense of returns an empty tuple, and
    there is no input that raises. That is a requirement rather than a courtesy -- the caller
    is a periodic loop beside reconcile, activity and trust, and a rule that can raise on a
    reading it did not expect takes the loop's other work down with it on a timer.

    `previous` is `None` on the service's first tick, when there is a reading and nothing to
    compare it against. That is also the accepted cost of holding the baseline in memory: an
    early reset in the minutes around a restart is missed. Missed, never invented.

    The results come back in the order the *current* reading publishes its windows, which is
    the order the limits block already renders them in. A retry therefore composes the same
    sentence twice rather than reshuffling a message the owner has already read.
    """
    if previous is None or current is None:
        return ()
    if previous.profile_id != current.profile_id:
        return ()
    # An absence is a declared silence, and the two exclusive things it may not become are a
    # baseline and a conclusion (DEC-061). Checked explicitly rather than relied upon to be an
    # empty `windows`, because the guard's reason is the type, not the emptiness.
    if previous.absence is not None or current.absence is not None:
        return ()
    if previous.stale_source != current.stale_source:
        return ()
    # Both stamps say when the *provider* recorded its figures. A file that has not been
    # rewritten carries the same stamp however often it is read, so a pair that did not advance
    # is either one observation twice or an older one arriving late -- and a late arrival of a
    # reading taken before the window filled up is indistinguishable, here, from a wipe.
    # Accepted cost: a provider that publishes no `observed_at` is never compared at all.
    if not _strictly_after(current.observed_at, previous.observed_at):
        return ()

    baseline = _windows_by_label(previous.windows)
    threshold = now + EARLY_RESET_GRACE
    detected: list[EarlyReset] = []
    for label, window in _windows_by_label(current.windows).items():
        before = baseline.get(label)
        if before is None or before.resets_at is None:
            continue
        if not _strictly_after(before.resets_at, threshold):
            continue
        if window.used_percent > before.used_percent - MINIMUM_DROP_POINTS:
            continue
        detected.append(
            EarlyReset(
                label=label,
                previous_percent=before.used_percent,
                current_percent=window.used_percent,
                expected_reset_at=before.resets_at,
            )
        )
    return tuple(detected)


def _windows_by_label(windows: tuple[UsageWindow, ...]) -> dict[str, UsageWindow]:
    """One reading's windows keyed by the provider's own label, with ambiguity dropped.

    Neither provider publishes two windows under one label today. If one starts, pairing them
    across readings becomes a guess about which row the earlier one was, so both are dropped
    and the label goes unreported -- the same direction every other guard here fails in.
    """
    seen: dict[str, UsageWindow] = {}
    ambiguous: set[str] = set()
    for window in windows:
        if window.label in seen:
            ambiguous.add(window.label)
        seen[window.label] = window
    return {label: window for label, window in seen.items() if label not in ambiguous}


def _strictly_after(later: datetime | None, earlier: datetime | None) -> bool:
    """Whether one instant is known to follow another, answering `False` when it is not known.

    The `TypeError` is the point. The two instants reach this module from different places --
    one parsed out of a provider's file, one taken from the service's own clock -- so a reader
    that ever emits a naive datetime beside an aware one would otherwise raise from inside a
    comparison, on a timer, for a notification nobody needs. Unknown and incomparable are both
    answered the same way, because both mean this rule cannot say the reading advanced.
    """
    if later is None or earlier is None:
        return False
    try:
        return later > earlier
    except TypeError:
        return False
