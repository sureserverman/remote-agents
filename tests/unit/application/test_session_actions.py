"""The one authority over which lifecycle actions a session offers."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from remote_agents.application.session_actions import (
    _FORCE_FAILURES,
    _GRACEFUL_FAILURES,
    GRACEFUL_TIMEOUT,
    OWNERSHIP_LOST,
    UNKNOWN_SESSION,
    StopFailure,
    available_actions,
    force_stop_failure,
    notifiable,
    stop_failure,
)
from remote_agents.domain.models import OrphanProvenance, SessionState

# Enumerated reflectively so a state added to the enum later fails here until it is
# classified, rather than silently inheriting whatever the last branch returned.
#
# Keyed on the *conservative* provenance. Every state but ORPHANED ignores the second
# argument entirely, and ORPHANED's adopted branch is the one exception, pinned separately
# below rather than folded in here — a single table keyed on both axes would have six rows
# that exist only to say "provenance changed nothing".
EXPECTED: dict[SessionState, tuple[str, ...]] = {
    SessionState.STARTING: (),
    SessionState.RUNNING: ("graceful", "force"),
    SessionState.STOP_REQUESTED: ("force",),
    SessionState.PRESERVED: ("cleanup", "force"),
    SessionState.FAILED: ("force",),
    SessionState.ENDED: (),
    # The ambiguous branch, and the branch every row written before migration 6 falls to.
    # The evidence supports no action, so none is offered (DEC-020).
    SessionState.ORPHANED: (),
}

CONSERVATIVE = (None, OrphanProvenance.AMBIGUOUS)
"""The two provenances that must render identically: unknown, and known-to-be-ambiguous."""


def test_every_state_is_classified() -> None:
    assert set(EXPECTED) == set(SessionState)


@pytest.mark.parametrize("provenance", CONSERVATIVE)
@pytest.mark.parametrize("state", list(SessionState))
def test_available_actions_for_every_state(
    state: SessionState, provenance: OrphanProvenance | None
) -> None:
    assert available_actions(state, provenance) == EXPECTED[state]


@pytest.mark.parametrize("state", list(SessionState))
def test_only_known_actions_are_ever_offered(state: SessionState) -> None:
    for provenance in (None, *OrphanProvenance):
        assert set(available_actions(state, provenance)) <= {"graceful", "cleanup", "force"}


@pytest.mark.parametrize("state", list(SessionState))
def test_graceful_only_from_running(state: SessionState) -> None:
    assert ("graceful" in available_actions(state, None)) is (state is SessionState.RUNNING)


@pytest.mark.parametrize("state", list(SessionState))
def test_cleanup_only_from_preserved(state: SessionState) -> None:
    assert ("cleanup" in available_actions(state, None)) is (state is SessionState.PRESERVED)


@pytest.mark.parametrize("state", list(SessionState))
def test_force_reconciles_the_two_prior_copies(state: SessionState) -> None:
    """The token issuer's set wins over the list builder's force-from-everything.

    ORPHANED is no longer decided by state alone — DEC-020 splits it — so this pins the four
    states whose answer provenance never touches, and the ORPHANED pair is pinned below.
    """
    forceable = {
        SessionState.RUNNING,
        SessionState.STOP_REQUESTED,
        SessionState.PRESERVED,
        SessionState.FAILED,
    }
    assert ("force" in available_actions(state, None)) is (state in forceable)


# DEC-020 — ORPHANED is two situations ----------------------------------------------------


def test_an_adopted_orphan_offers_force_and_only_force() -> None:
    """The live agent the database lost. Force is the action its pane actually supports.

    Only force: graceful needs a managed pane this app can address by its own record, and
    cleanup is about a *preserved* pane's retained output. An adopted record has neither.
    """
    assert available_actions(SessionState.ORPHANED, OrphanProvenance.ADOPTED) == ("force",)


@pytest.mark.parametrize("provenance", CONSERVATIVE)
def test_a_muddled_evidence_orphan_still_offers_nothing(
    provenance: OrphanProvenance | None,
) -> None:
    """The half of DEC-020 that is a refusal, and the half a later edit is likeliest to lose.

    A row that predates migration 6 reads `None`, and it must render exactly as a known
    ambiguous one does — otherwise the migration's conservative default would be a different
    product experience from the branch it was chosen to imitate.
    """
    assert available_actions(SessionState.ORPHANED, provenance) == ()


@pytest.mark.parametrize(
    "state", [state for state in SessionState if state is not SessionState.ORPHANED]
)
def test_provenance_changes_nothing_for_any_state_but_orphaned(state: SessionState) -> None:
    """The new parameter is inert everywhere else, so a caller passing it cannot skew a row.

    Worth pinning because the argument is now threaded through seven call sites: if it ever
    started mattering for, say, PRESERVED, a surface that passes it and one that does not
    would silently disagree — the DEC-007 divergence the parity contract exists to catch.
    """
    answers = {available_actions(state, provenance) for provenance in (None, *OrphanProvenance)}

    assert len(answers) == 1


def test_a_starting_session_offers_nothing() -> None:
    assert available_actions(SessionState.STARTING, None) == ()


def test_an_ended_session_offers_nothing() -> None:
    assert available_actions(SessionState.ENDED, None) == ()


def test_ordering_is_stable_and_puts_force_last() -> None:
    """Force is the destructive option; it never leads a menu."""
    for state in SessionState:
        for provenance in (None, *OrphanProvenance):
            actions = available_actions(state, provenance)
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
    graceful = [
        stop_failure(_Observation(preserved=False, detail=cause))
        for cause in (UNKNOWN_SESSION, GRACEFUL_TIMEOUT)
    ]
    forced = [force_stop_failure(_Observation(preserved=False, detail=OWNERSHIP_LOST))]
    failures = [failure for failure in graceful + forced if failure is not None]
    assert len(failures) == 3, "a cause stopped being recognised by its own reader"

    summaries = {failure.summary for failure in failures}
    remedies = {failure.remedy for failure in failures}
    assert len(summaries) == 3, f"two causes share a summary: {summaries}"
    assert len(remedies) == 3, f"two causes share a remedy: {remedies}"


def test_neither_reader_can_reach_the_other_s_causes() -> None:
    """The disjointness the two readers depend on, enforced rather than assumed.

    Both used to read one table, so they were disjoint only because `TmuxRuntime.graceful_stop`
    and `TmuxRuntime.force_stop` happen to emit different strings. A graceful stop that ever
    reported `ownership_lost` would have been handed force's sentence — "the record has been
    cleared... look for it with tmux" — over a session that is still sitting in the list. Found
    by the Stage 3 gate's Tier-2 review, which noted nothing typed or tested the partition.

    Asserted over the tables rather than over a sample of details, so a fourth cause added to
    either one cannot land in both without failing here.
    """
    assert not _GRACEFUL_FAILURES.keys() & _FORCE_FAILURES.keys(), (
        "a cause is reachable through both readers, so one of them can render wording written "
        "for the other action"
    )
    for cause in _FORCE_FAILURES:
        assert stop_failure(_Observation(preserved=False, detail=cause)) == StopFailure(
            cause,
            "The terminal did not report a clean exit.",
            f"The terminal reported {cause!r} and the session was left as it is. "
            "Force stop it if you need it ended now.",
        ), "graceful's reader recognised a force cause instead of falling back"
    for cause in _GRACEFUL_FAILURES:
        assert force_stop_failure(_Observation(preserved=False, detail=cause)) is None, (
            "force's reader recognised a graceful cause"
        )


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


def test_an_unrecognised_force_detail_is_reported_to_the_log_but_a_clean_kill_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failing open quietly is not the same as failing open. Found by the Stage 3 evaluator.

    An empty detail is the ordinary kill and must stay silent — logging there would warn on
    every successful force. A detail this table does not know is a cause somebody added without
    coming here, and answering `None` for it makes both surfaces report "the session has ended"
    over an observation nobody has read. On the one path that kills, that is worth a line in
    the log; the alternative is that the defect BL-026 closed returns silently.
    """
    with caplog.at_level(logging.WARNING):
        assert force_stop_failure(_Observation(preserved=False, detail="")) is None
    assert not caplog.records, "a completed force stop must not warn"

    with caplog.at_level(logging.WARNING):
        assert force_stop_failure(_Observation(preserved=False, detail="something_new")) is None
    assert "something_new" in caplog.text, (
        "a cause nobody has words for was reported as a completed kill with nothing said"
    )


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


# `notifiable` — which sessions an unprompted notification can still be news about ---------


@pytest.mark.parametrize("state", list(SessionState))
def test_notifiable_answers_for_every_state_and_only_a_working_one_is_news(
    state: SessionState,
) -> None:
    """The predicate is total, and exactly the two working states answer True.

    Parametrized over the enum rather than over a hand-written list of states, for the reason
    `EXPECTED` is: a state added later must fail here until somebody decides what a
    notification about it would claim, instead of silently inheriting a `False` nobody chose.

    STARTING is in the True half deliberately and is the half an edit is likeliest to lose —
    it looks like the not-working-yet state, and dropping it would discard a real agent's
    first report whenever its hook beats reconciliation to the record.
    """
    expected = state in {SessionState.STARTING, SessionState.RUNNING}

    assert notifiable(state) is expected


@pytest.mark.parametrize(
    "state",
    [
        SessionState.STOP_REQUESTED,
        SessionState.PRESERVED,
        SessionState.FAILED,
        SessionState.ENDED,
        SessionState.ORPHANED,
    ],
)
def test_a_session_the_owner_has_already_dealt_with_is_never_notified_about(
    state: SessionState,
) -> None:
    """The complaint that produced the predicate, pinned as its own assertion.

    Each of these is a session whose stopping the owner either did themselves or has already
    watched, so a notification announcing it tells them their own action back. Named
    separately from the total test above so a regression here reads as the product defect it
    is rather than as a table that stopped matching.
    """
    assert notifiable(state) is False
