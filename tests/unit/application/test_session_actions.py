"""The one authority over which lifecycle actions a session offers."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from remote_agents.application.session_actions import (
    GRACEFUL_TIMEOUT,
    OWNERSHIP_LOST,
    UNKNOWN_SESSION,
    available_actions,
    force_stop_failure,
    stop_failure,
)
from remote_agents.domain.models import SessionState

# Enumerated reflectively so a state added to the enum later fails here until it is
# classified, rather than silently inheriting whatever the last branch returned.
EXPECTED: dict[SessionState, tuple[str, ...]] = {
    SessionState.STARTING: (),
    SessionState.RUNNING: ("graceful", "force"),
    SessionState.STOP_REQUESTED: ("force",),
    SessionState.PRESERVED: ("cleanup", "force"),
    SessionState.FAILED: ("force",),
    SessionState.ENDED: (),
    # Not forceable: the domain has no transition out of ORPHANED, so offering force here
    # would raise InvalidTransition rather than stop anything. See
    # tests/architecture/test_policy_matches_domain.py.
    SessionState.ORPHANED: (),
}


def test_every_state_is_classified() -> None:
    assert set(EXPECTED) == set(SessionState)


@pytest.mark.parametrize("state", list(SessionState))
def test_available_actions_for_every_state(state: SessionState) -> None:
    assert available_actions(state) == EXPECTED[state]


@pytest.mark.parametrize("state", list(SessionState))
def test_only_known_actions_are_ever_offered(state: SessionState) -> None:
    assert set(available_actions(state)) <= {"graceful", "cleanup", "force"}


@pytest.mark.parametrize("state", list(SessionState))
def test_graceful_only_from_running(state: SessionState) -> None:
    assert ("graceful" in available_actions(state)) is (state is SessionState.RUNNING)


@pytest.mark.parametrize("state", list(SessionState))
def test_cleanup_only_from_preserved(state: SessionState) -> None:
    assert ("cleanup" in available_actions(state)) is (state is SessionState.PRESERVED)


@pytest.mark.parametrize("state", list(SessionState))
def test_force_reconciles_the_two_prior_copies(state: SessionState) -> None:
    """The token issuer's set wins over the list builder's force-from-everything.

    ORPHANED stays out. It is the one state the two copies never agreed on, and the domain
    settles it: no event is legal from ORPHANED, so a force there raises rather than stops.
    """
    forceable = {
        SessionState.RUNNING,
        SessionState.STOP_REQUESTED,
        SessionState.PRESERVED,
        SessionState.FAILED,
    }
    assert ("force" in available_actions(state)) is (state in forceable)


def test_a_starting_session_offers_nothing() -> None:
    assert available_actions(SessionState.STARTING) == ()


def test_an_ended_session_offers_nothing() -> None:
    assert available_actions(SessionState.ENDED) == ()


def test_ordering_is_stable_and_puts_force_last() -> None:
    """Force is the destructive option; it never leads a menu."""
    for state in SessionState:
        actions = available_actions(state)
        if "force" in actions:
            assert actions[-1] == "force"


# `stop_failure` — the shared vocabulary both surfaces render -----------------------------
#
# Tested here rather than only through the two adapters, which is where its whole coverage
# lived until a gate review said so. `tests/contract/test_stop_result_parity.py` proves the
# surfaces agree and parametrizes over the two *known* causes, so the decision this function
# actually makes — is this a failure at all — and its fallback for a cause nobody has words
# for were exercised nowhere in the tree.


@dataclass(frozen=True, slots=True)
class _Observation:
    """The two fields `stop_failure` reads. Any `TerminalObservation` satisfies this."""

    preserved: bool
    detail: str = ""


def test_a_preserved_stop_is_not_a_failure() -> None:
    """`preserved` is the whole test for success, and it is the one that must not drift.

    A surface that reported every stop as suspect would be worse than one that reported none:
    the owner would learn to ignore it, which is where BL-008 started.
    """
    assert stop_failure(_Observation(preserved=True)) is None
    assert stop_failure(_Observation(preserved=True, detail=GRACEFUL_TIMEOUT)) is None, (
        "a stale detail on a preserved observation must not manufacture a failure"
    )


@pytest.mark.parametrize("detail", [UNKNOWN_SESSION, GRACEFUL_TIMEOUT])
def test_each_known_cause_gets_its_own_words(detail: str) -> None:
    failure = stop_failure(_Observation(preserved=False, detail=detail))
    assert failure is not None
    assert failure.detail == detail
    assert failure.summary and failure.remedy


def test_no_two_known_causes_share_wording() -> None:
    """The requirement is that they cannot be mistaken for each other, at the source.

    The contract test asserts this of the *rendered* surfaces, which is where it matters; this
    asserts it of the vocabulary itself, so a convergence is caught before either surface has
    a chance to render it identically.

    Enumerated over the whole table rather than over the pair it was written for. When
    `ownership_lost` joined it for BL-026 this test still compared exactly two causes and would
    have kept passing had the third been written in the second's words — a pairwise assertion
    over a growing table only ever checks the pair somebody remembered to name.
    """
    causes = [UNKNOWN_SESSION, GRACEFUL_TIMEOUT, OWNERSHIP_LOST]
    failures = [stop_failure(_Observation(preserved=False, detail=cause)) for cause in causes]
    assert all(failure is not None for failure in failures)

    summaries = {failure.summary for failure in failures if failure is not None}
    remedies = {failure.remedy for failure in failures if failure is not None}
    assert len(summaries) == len(causes), f"two causes share a summary: {summaries}"
    assert len(remedies) == len(causes), f"two causes share a remedy: {remedies}"


def test_force_reads_the_detail_because_every_force_leaves_preserved_false() -> None:
    """`stop_failure` cannot read a force, and this is the line that says so out loud.

    Force removes the pane, so `preserved` is false on the successful outcome too — the field
    `stop_failure` keys on carries no signal here. Handing it a force observation would report
    every completed kill as a failure, which is why BL-026's fix is a second reader rather than
    a second caller of the first.
    """
    killed = _Observation(preserved=False)
    found_nothing = _Observation(preserved=False, detail=OWNERSHIP_LOST)

    assert force_stop_failure(killed) is None, "a completed force stop has nothing to report"
    assert stop_failure(killed) is not None, (
        "the sibling still reads the same observation as a failure — which is exactly why "
        "force must not be routed through it"
    )

    lost = force_stop_failure(found_nothing)
    assert lost is not None
    assert lost.detail == OWNERSHIP_LOST
    assert lost.summary and lost.remedy


def test_an_unrecognised_detail_is_not_a_force_failure() -> None:
    """The mirror image of the sibling's fail-closed default, and deliberately so.

    `stop_failure` treats an unknown detail as a failure because `preserved` false already
    established that the exit sequence did not work. Nothing establishes that here: force sets
    a detail only when it found no pane, and the ordinary kill carries none. Defaulting the
    unknown to a failure would announce one on every successful force the moment some unrelated
    detail is added.
    """
    assert force_stop_failure(_Observation(preserved=False, detail="something_new")) is None


@pytest.mark.parametrize("detail", ["", "something_new"])
def test_an_unrecognised_cause_is_still_a_failure(detail: str) -> None:
    """The fail-dangerous default, closed — and the branch nothing else in the tree reaches.

    `preserved` false means the exit sequence did not work whatever the terminal called the
    reason. Answering `None` here would report a stop that did nothing as a stop that
    succeeded, which is the defect in `_issue_stop`'s old trailing `else` written one layer up.
    """
    failure = stop_failure(_Observation(preserved=False, detail=detail))
    assert failure is not None
    assert failure.detail == detail
    assert repr(detail) in failure.remedy, (
        "a cause nobody has a sentence for must still name itself, or it cannot be traced"
    )


def test_the_fallback_does_not_stutter_when_a_surface_prefixes_it() -> None:
    """The TUI renders `f"{label} did not take effect. {summary}"`, so the summary cannot.

    A gate evaluator read the composed line: "Stop and close did not take effect. The stop did
    not take effect." Pinned here rather than in the surface, because the surface is only the
    place it was noticed — the constraint belongs to whoever writes the summary.
    """
    failure = stop_failure(_Observation(preserved=False, detail="something_new"))
    assert failure is not None
    assert "did not take effect" not in failure.summary
