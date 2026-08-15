"""Every lifecycle situation explains itself, and no explanation contradicts the situation."""

from __future__ import annotations

import pytest

from remote_agents.application.session_actions import available_actions, explain_state
from remote_agents.domain.models import OrphanProvenance, SessionState

# A *situation*, not a state. DEC-020 made ORPHANED two of them, so a sweep over
# `SessionState` alone would leave the adopted branch — the only one offering a destructive
# action — with no case at all. The conservative provenances collapse into the single
# ORPHANED situation deliberately: they must read identically, and a test below pins that.
SITUATIONS: list[tuple[SessionState, OrphanProvenance | None]] = [
    *((state, None) for state in SessionState),
    (SessionState.ORPHANED, OrphanProvenance.ADOPTED),
]


@pytest.mark.parametrize(("state", "provenance"), SITUATIONS)
def test_every_situation_has_a_non_empty_explanation(
    state: SessionState, provenance: OrphanProvenance | None
) -> None:
    assert explain_state(state, provenance).strip()


def test_no_two_situations_share_an_explanation() -> None:
    """A shared string means at least one situation is being described as something it is not.

    The two ORPHANED branches are in here as separate entries on purpose: DEC-020 is a
    *capability* decision, so if both branches render the same sentence the branch exists in
    the code and not in the product, which is exactly what the Stage 4 gate asks a reader.
    """
    explanations = [explain_state(state, provenance) for state, provenance in SITUATIONS]

    assert len(set(explanations)) == len(explanations)


def test_a_starting_session_is_not_described_as_inactive() -> None:
    """The bot's fallback said 'no longer active' for STARTING, which is the opposite."""
    text = explain_state(SessionState.STARTING, None).casefold()
    assert "no longer" not in text
    assert "starting" in text or "coming up" in text


@pytest.mark.parametrize(
    ("state", "provenance"),
    [(SessionState.ENDED, None), (SessionState.ORPHANED, None)],
)
def test_states_that_offer_no_action_say_why(
    state: SessionState, provenance: OrphanProvenance | None
) -> None:
    assert explain_state(state, provenance).strip()


@pytest.mark.parametrize("provenance", [None, OrphanProvenance.AMBIGUOUS])
def test_a_muddled_evidence_orphan_promises_no_stop_that_cannot_happen(
    provenance: OrphanProvenance | None,
) -> None:
    """The conservative branch offers nothing, so its text must not imply otherwise.

    Scoped to the conservative branch. Before DEC-020 this covered ORPHANED outright, which
    is now only half the state — and asserting it of the adopted branch would assert the
    opposite of the decision.
    """
    text = explain_state(SessionState.ORPHANED, provenance).casefold()

    assert available_actions(SessionState.ORPHANED, provenance) == ()
    assert "force" not in text
    assert "stop" not in text


def test_an_adopted_orphan_names_the_one_action_it_does_offer() -> None:
    """The inverse of its sibling above, and the reason that one had to be narrowed.

    An adopted record offers force, so a text that avoided the word — as the single shared
    ORPHANED sentence did — would leave the owner with a destructive button and a sentence
    saying no action is offered for it.
    """
    text = explain_state(SessionState.ORPHANED, OrphanProvenance.ADOPTED).casefold()

    assert "force" in available_actions(SessionState.ORPHANED, OrphanProvenance.ADOPTED)
    assert "force stop" in text
    assert "no action is offered" not in text


def test_neither_orphan_branch_needs_the_word_provenance_to_be_understood() -> None:
    """The Stage 4 gate asks whether a reader can tell the branches apart without the jargon.

    The register's vocabulary is for the register. What the owner needs from the adopted
    branch is that something is probably still running and that force is what reaches it.
    """
    for provenance in (None, *OrphanProvenance):
        text = explain_state(SessionState.ORPHANED, provenance).casefold()
        assert "provenance" not in text
        assert "adopted" not in text
        assert "ambiguous" not in text
