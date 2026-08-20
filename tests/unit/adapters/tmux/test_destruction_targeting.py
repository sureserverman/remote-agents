"""A stop destroys the agent's own pane, wherever it is, and cleans up only what is ours.

This is the operation the whole sub-plan was ordered around. `kill-session` on a session
target destroys whatever window that session holds — so with the agent's pane hosted
elsewhere, the record reaches ENDED while the agent keeps running, which is the one failure
mode a stop must never have (DEC-006: a session's stop must not depend on the process that
launched it, and DEC-022: a stop that was never sent is its own event, not a silent success).

Addressing the pane inverts that: the kill lands on the agent and nothing else. What it
leaves behind needs care in the other direction. Verified on tmux 3.4 (2026-08-19), killing
a window's last pane destroys the window and its session, so the common case cleans itself
up. The uncommon ones must not over-reach:

- A home session still holding another pane is **not** emptied by us. Under the console
  that pane is the projects surface, parked there while the agent is displayed; killing the
  session would take a live surface process with it. A hand-grown pane is equally not ours.
- A schema-1 session has no pane to address and takes the `kill-session` path unchanged.
"""

from __future__ import annotations

import pytest

from remote_agents.domain.models import SessionId
from remote_agents.ports.terminal import TerminalTargetMissing

from .support_targeting import TargetingRunner, gateway_for, pane_line

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_OTHER = SessionId.parse("fedcba98-7654-3210-fedc-ba9876543210")
_SESSION_TARGET = "ra-01234567-89ab-cdef-0123-456789abcdef:"
_BASE = ("tmux", "-L", "remote-agents-test-target")


def killed(runner: TargetingRunner) -> list[tuple[str, ...]]:
    return [call for call in runner.calls if "kill-pane" in call or "kill-session" in call]


async def test_destruction_kills_the_resolved_pane() -> None:
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),))
    await gateway_for(runner).destroy(_SESSION)
    assert killed(runner)[0] == (*_BASE, "kill-pane", "-t", "%3")


async def test_destruction_reaches_a_pane_the_console_is_hosting() -> None:
    """The failure this sub-plan exists to remove: a session target here would destroy the
    agent's empty home window and leave the agent itself running in the console."""
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),), host="ra-console")
    await gateway_for(runner).destroy(_SESSION)
    assert killed(runner) == [(*_BASE, "kill-pane", "-t", "%3")]


async def test_a_legacy_session_is_killed_by_pane_too() -> None:
    """Schema 1 does not get the session-wide kill, and this is the correction that
    matters most in this file.

    A schema-1 mark is inherited rather than the pane's own, which is why `pane_for` will
    not offer it as an address for *typing*. But the pane id on that line is a real pane in
    that session's own window, and killing it is strictly narrower than killing the session
    around it — verified on tmux 3.4, `kill-session` on a session whose window is linked
    into another (which the shipped console does for every live session) removes the name,
    exits 0, and leaves the agent running. A legacy session is exactly the kind most likely
    to have been open as a tab when the owner stops it.
    """
    runner = TargetingRunner(panes=(("%3", _SESSION, "1"),))
    await gateway_for(runner).destroy(_SESSION)
    assert killed(runner) == [(*_BASE, "kill-pane", "-t", "%3")]


async def test_only_an_identity_with_no_pane_at_all_reaches_the_session_wide_kill() -> None:
    """The remaining `kill-session` and its accepted cost, stated rather than implied.

    It destroys every window the session owns and it cannot reach a pane whose window is
    linked elsewhere — both worse than `kill-pane`. It survives only where the inventory
    decoded no pane for this identity, so there is nothing narrower to name, and the
    alternative to a wide kill is not stopping the agent at all.
    """
    runner = TargetingRunner(panes=(("%9", _OTHER, "2"),))
    await gateway_for(runner).destroy(_SESSION)
    assert killed(runner) == [(*_BASE, "kill-session", "-t", _SESSION_TARGET)]


async def test_a_session_with_no_evidence_at_all_still_takes_the_kill_session_path() -> None:
    """Nothing resolved and nothing listed: the record may still name a session tmux knows
    about, and the caller asked for it to be gone."""
    runner = TargetingRunner(panes=())
    await gateway_for(runner).destroy(_SESSION)
    assert killed(runner) == [(*_BASE, "kill-session", "-t", _SESSION_TARGET)]


async def test_the_home_session_is_not_emptied_when_something_else_is_living_in_it() -> None:
    """The parked projects surface, or anybody's hand-grown pane. Killing the session to
    tidy up would destroy a process this service never started."""
    runner = TargetingRunner(
        panes=(("%3", _SESSION, "2"),),
        extra_lines=("|".join((f"ra-{_SESSION}", "$1", "%8", "300", "0", "", "", "", "", "")),),
    )
    await gateway_for(runner).destroy(_SESSION)
    assert killed(runner) == [(*_BASE, "kill-pane", "-t", "%3")]


async def test_killing_the_pane_is_the_whole_destruction_and_nothing_chases_it() -> None:
    """No second call, and that is a decision rather than an omission.

    The task as written also removed "the home session if nothing managed remains in it", to
    avoid leaving a stray empty session behind a dead agent. Probed on tmux 3.4: a session
    whose last pane is killed is destroyed with it, so that stray state does not exist. The
    only home session that survives a `kill-pane` is one still holding *another* pane — the
    parked projects surface, or somebody's hand-grown split — and killing that session
    destroys a process this service never started. So the clause is dropped: there is
    nothing to tidy that tmux has not already tidied, and every case where the tidy would
    fire is a case where it would do harm.
    """
    runner = TargetingRunner(panes=(("%3", _SESSION, "2"),))
    await gateway_for(runner).destroy(_SESSION)
    assert killed(runner) == [(*_BASE, "kill-pane", "-t", "%3")]
    assert not [call for call in runner.calls if "kill-session" in call]


async def test_a_pane_already_gone_is_reported_as_missing_not_as_a_failure() -> None:
    runner = TargetingRunner(
        panes=(("%3", _SESSION, "2"),), fail_with="tmux command failed: can't find pane: %3"
    )
    with pytest.raises(TerminalTargetMissing):
        await gateway_for(runner).destroy(_SESSION)


async def test_a_failed_resolution_refuses_to_destroy_anything() -> None:
    """The same rule capture and send-keys follow, and it matters most here: not knowing
    where the agent is must never become killing whatever occupies its window."""
    runner = TargetingRunner(
        panes=(("%3", _SESSION, "2"),),
        listing_fails="tmux command failed: can't find session: ra-x",
    )
    with pytest.raises(TerminalTargetMissing):
        await gateway_for(runner).destroy(_SESSION)
    assert killed(runner) == []


async def test_a_hand_split_legacy_session_never_kills_by_listing_order() -> None:
    """The reproduction that blocked this task twice, pinned.

    A schema-1 mark is session-scoped, so tmux's pane → session fallback hands the agent's
    identity to *every* pane in that window — including one an operator split by hand. And
    `split-window -b` puts that pane first in `list-panes -a`, verified on tmux 3.4:

        ra-<uuid>|%1|idx=0|schema=1|id=<uuid>    <- the operator's pane, listed first
        ra-<uuid>|%0|idx=1|schema=1|id=<uuid>    <- the agent's own pane

    A draft that trusted the first decoded pane killed `%1` and left the agent running: an
    ENDED record over a live process, plus a bystander destroyed. Both panes are killed
    instead — the same set `kill-session` would have taken, without its inability to reach a
    linked window. `%0` being in the set is the assertion that matters.
    """
    operators_pane = pane_line("%1", _SESSION, "1")
    agents_pane = pane_line("%0", _SESSION, "1")
    runner = TargetingRunner(panes=(), extra_lines=(operators_pane, agents_pane))

    await gateway_for(runner).destroy(_SESSION)

    assert killed(runner) == [
        (*_BASE, "kill-pane", "-t", "%1"),
        (*_BASE, "kill-pane", "-t", "%0"),
    ]


async def test_an_owned_mark_wins_over_inherited_ones_and_kills_only_that_pane() -> None:
    """Schema 2 identifies one pane, so precision is available and is used: a session that
    has been upgraded does not pay the wide kill an inherited mark forces.

    The mixed state fixtured here is not reachable in production — nothing upgrades a
    schema-1 session in place, so a session's panes are all-owned or all-inherited. It is
    pinned anyway because the *rule* is what is being fixed: precision is preferred wherever
    it exists, rather than the wide kill being the default that precision has to argue with.
    """
    runner = TargetingRunner(
        panes=(("%0", _SESSION, "2"),),
        extra_lines=(pane_line("%1", _SESSION, "1"),),
    )

    await gateway_for(runner).destroy(_SESSION)

    assert killed(runner) == [(*_BASE, "kill-pane", "-t", "%0")]


async def test_a_later_kill_finding_nothing_after_an_earlier_one_landed_is_success() -> None:
    """A later `kill-pane` answering "already gone" once an earlier one has landed is the
    operation succeeding, whatever the cause — a window taken by the first kill, or a race
    with something else retiring the pane. Only "already gone" is tolerated: any other
    failure raises, because a stop that did not reach the agent must not read as one that
    did (DEC-006), and some *other* pane's kill succeeding is not evidence about this one.
    """
    runner = TargetingRunner(
        panes=(),
        extra_lines=(pane_line("%1", _SESSION, "1"), pane_line("%0", _SESSION, "1")),
        fail_after_first_kill="tmux command failed: can't find pane: %0",
    )

    await gateway_for(runner).destroy(_SESSION)

    assert len(killed(runner)) == 2
