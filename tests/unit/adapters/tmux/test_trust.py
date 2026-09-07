from remote_agents.adapters.tmux.trust import classify_trust_capture, plan_trust_keys
from remote_agents.domain.trust import TrustState

# The dialog Claude Code 2.1.263 actually draws, captured from a real pane at 120 columns.
# Kept whole rather than trimmed to the matched markers: a classifier tuned to a fragment of
# the thing it classifies is only tested against its own assumptions.
#
# One line is not verbatim: the workspace path was replaced with a synthetic one before this
# repository was published, because the original named an unrelated project of the author's.
# Neither the classifier nor the key planner reads that line, so the substitution costs the
# fixture nothing it was testing.
#
# **Note what changed from the dialog this module was first written against** (kept below as
# `_LEGACY_PROMPT`): the options are unnumbered, "No, exit" comes first, and the cursor rests
# on it. That is the whole defect — a fixed Enter answers *no* here.
_REAL_PROMPT = """
────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:

 /home/user/dev/example/sample-project

 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""

# The dialog as it stood when this module was written: numbered, affirmative first, and
# pre-selected. Retained because a host in the field may still be running it, and because the
# planner's claim is that it reads *either* layout rather than that it swapped one fixed
# assumption for another.
_LEGACY_PROMPT = """
────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:

 /home/user/dev/example/sample-project

 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""


def test_the_real_dialog_is_recognised() -> None:
    assert classify_trust_capture(_REAL_PROMPT) is TrustState.AWAITING
    assert classify_trust_capture(_LEGACY_PROMPT) is TrustState.AWAITING


def test_classification_fails_closed_for_ordinary_output() -> None:
    assert classify_trust_capture("Claude Code") is TrustState.UNKNOWN
    assert classify_trust_capture("") is TrustState.UNKNOWN


def test_either_marker_alone_is_not_enough() -> None:
    """The two-marker rule, which is the whole defence against a false positive.

    An agent discussing this feature emits one marker or the other constantly -- the test
    file you are reading contains both. Classifying on either alone would let a session
    talking *about* the trust prompt be reported as blocked *on* it, and the owner would be
    offered a button that sends Enter into a working agent.
    """
    assert classify_trust_capture("Is this a project you created or one you trust?") is (
        TrustState.UNKNOWN
    )
    assert classify_trust_capture("Yes, I trust this folder") is TrustState.UNKNOWN


def test_the_affirmative_option_must_follow_the_question() -> None:
    """Order matters, so a transcript quoting the answer above an unrelated question fails.

    `find` is anchored at the question's position rather than searched globally for exactly
    this: the two markers appearing anywhere in one capture is a weaker claim than the
    dialog being on screen, and the pane is what the button acts on.
    """
    reversed_order = (
        "Yes, I trust this folder\n...much earlier output...\n"
        "Is this a project you created or one you trust?"
    )

    assert classify_trust_capture(reversed_order) is TrustState.UNKNOWN


def test_the_answer_walks_down_to_yes_when_the_cursor_rests_on_no() -> None:
    """The defect, stated as the test that would have caught it.

    A bare Enter into this dialog picks "No, exit" and the agent exits -- verified against a
    live pane, which is where the fixture above came from. The plan has to move first.
    """
    assert plan_trust_keys(_REAL_PROMPT) == ("Down", "Enter")


def test_the_answer_is_a_bare_confirm_when_the_cursor_already_rests_on_yes() -> None:
    """The legacy layout still gets exactly the keys it always got.

    Not a nicety: the planner's argument is that it reads the screen rather than that the
    right answer is now "one down". A host on the older dialog must not be walked past the
    option it is already sitting on.
    """
    assert plan_trust_keys(_LEGACY_PROMPT) == ("Enter",)


def test_the_answer_walks_up_when_yes_sits_above_the_cursor() -> None:
    """Direction is computed, not assumed, so a third ordering needs no third release."""
    upward = _REAL_PROMPT.replace(
        " ❯ No, exit\n   Yes, I trust this folder",
        "   Yes, I trust this folder\n ❯ No, exit",
    )

    assert plan_trust_keys(upward) == ("Up", "Enter")


def test_no_keys_are_planned_for_a_pane_that_is_not_asking() -> None:
    """Fails closed into pressing *nothing*, which is the only safe default here.

    The keys this plans end in a confirming Enter, and an Enter into a working agent is a
    keypress into somebody's work. So every unreadable screen is `None` rather than a guess.
    """
    assert plan_trust_keys("Claude Code") is None
    assert plan_trust_keys("") is None


def test_no_keys_are_planned_when_the_cursor_cannot_be_found() -> None:
    """A dialog on screen is not the same fact as knowing which row is selected.

    Classification only needs the question and the option; the *answer* additionally needs
    the cursor, and a capture that lost it -- a torn read, a redraw caught mid-frame -- must
    not fall back to the fixed Enter this module exists to stop sending.
    """
    cursorless = _REAL_PROMPT.replace("❯", " ")

    assert classify_trust_capture(cursorless) is TrustState.AWAITING
    assert plan_trust_keys(cursorless) is None


def test_no_keys_are_planned_when_the_cursor_is_not_in_the_option_block() -> None:
    """Counting rows across a blank line is arithmetic over a layout nobody has seen.

    The chooser draws its options as adjacent rows. A cursor somewhere else on screen -- a
    prompt above, a menu that scrolled -- is not `n` rows from "Yes" in any sense the arrow
    keys would honour, so the count is refused rather than sent.
    """
    detached = _REAL_PROMPT.replace(
        " ❯ No, exit\n   Yes, I trust this folder",
        " ❯ No, exit\n\n   Yes, I trust this folder",
    )

    assert plan_trust_keys(detached) is None


def test_no_keys_are_planned_when_two_rows_offer_to_trust_the_folder() -> None:
    """Two affirmatives is a screen this cannot choose between, so it chooses neither."""
    doubled = _REAL_PROMPT.replace(
        "   Yes, I trust this folder",
        "   Yes, I trust this folder\n   Yes, I trust this folder",
    )

    assert plan_trust_keys(doubled) is None
