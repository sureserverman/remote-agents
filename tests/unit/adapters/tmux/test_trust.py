import pytest
from trust_captures import capture

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
    assert classify_trust_capture(_REAL_PROMPT, _dialog("claude")) is TrustState.AWAITING
    assert classify_trust_capture(_LEGACY_PROMPT, _dialog("claude")) is TrustState.AWAITING


def test_classification_fails_closed_for_ordinary_output() -> None:
    assert classify_trust_capture("Claude Code", _dialog("claude")) is TrustState.UNKNOWN
    assert classify_trust_capture("", _dialog("claude")) is TrustState.UNKNOWN


def test_either_marker_alone_is_not_enough() -> None:
    """The two-marker rule, which is the whole defence against a false positive.

    An agent discussing this feature emits one marker or the other constantly -- the test
    file you are reading contains both. Classifying on either alone would let a session
    talking *about* the trust prompt be reported as blocked *on* it, and the owner would be
    offered a button that sends Enter into a working agent.
    """
    assert classify_trust_capture(
        "Is this a project you created or one you trust?", _dialog("claude")
    ) is TrustState.UNKNOWN
    assert (
        classify_trust_capture("Yes, I trust this folder", _dialog("claude"))
        is TrustState.UNKNOWN
    )


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

    assert classify_trust_capture(reversed_order, _dialog("claude")) is TrustState.UNKNOWN


def test_the_answer_walks_down_to_yes_when_the_cursor_rests_on_no() -> None:
    """The defect, stated as the test that would have caught it.

    A bare Enter into this dialog picks "No, exit" and the agent exits -- verified against a
    live pane, which is where the fixture above came from. The plan has to move first.
    """
    assert plan_trust_keys(_REAL_PROMPT, _dialog("claude")) == ("Down", "Enter")


def test_the_answer_is_a_bare_confirm_when_the_cursor_already_rests_on_yes() -> None:
    """The legacy layout still gets exactly the keys it always got.

    Not a nicety: the planner's argument is that it reads the screen rather than that the
    right answer is now "one down". A host on the older dialog must not be walked past the
    option it is already sitting on.
    """
    assert plan_trust_keys(_LEGACY_PROMPT, _dialog("claude")) == ("Enter",)


def test_the_answer_walks_up_when_yes_sits_above_the_cursor() -> None:
    """Direction is computed, not assumed, so a third ordering needs no third release."""
    upward = _REAL_PROMPT.replace(
        " ❯ No, exit\n   Yes, I trust this folder",
        "   Yes, I trust this folder\n ❯ No, exit",
    )

    assert plan_trust_keys(upward, _dialog("claude")) == ("Up", "Enter")


def test_no_keys_are_planned_for_a_pane_that_is_not_asking() -> None:
    """Fails closed into pressing *nothing*, which is the only safe default here.

    The keys this plans end in a confirming Enter, and an Enter into a working agent is a
    keypress into somebody's work. So every unreadable screen is `None` rather than a guess.
    """
    assert plan_trust_keys("Claude Code", _dialog("claude")) is None
    assert plan_trust_keys("", _dialog("claude")) is None


def test_no_keys_are_planned_when_the_cursor_cannot_be_found() -> None:
    """A dialog on screen is not the same fact as knowing which row is selected.

    Classification only needs the question and the option; the *answer* additionally needs
    the cursor, and a capture that lost it -- a torn read, a redraw caught mid-frame -- must
    not fall back to the fixed Enter this module exists to stop sending.
    """
    cursorless = _REAL_PROMPT.replace("❯", " ")

    assert classify_trust_capture(cursorless, _dialog("claude")) is TrustState.AWAITING
    assert plan_trust_keys(cursorless, _dialog("claude")) is None


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

    assert plan_trust_keys(detached, _dialog("claude")) is None


def test_no_keys_are_planned_when_two_rows_offer_to_trust_the_folder() -> None:
    """Two affirmatives is a screen this cannot choose between, so it chooses neither."""
    doubled = _REAL_PROMPT.replace(
        "   Yes, I trust this folder",
        "   Yes, I trust this folder\n   Yes, I trust this folder",
    )

    assert plan_trust_keys(doubled, _dialog("claude")) is None


def test_declining_walks_to_the_negative_option_by_the_same_rule() -> None:
    """The other answer to the same question, planned the same way.

    The cursor rests on "No, exit" in the dialog Claude Code 2.1.263 draws, so declining it is
    a bare confirm — and that is exactly the keypress the accept path had to stop sending.
    Both plans come off one capture, so they cannot disagree about where the cursor is.
    """
    assert plan_trust_keys(_REAL_PROMPT, _dialog("claude"), accept=False) == ("Enter",)


def test_declining_the_legacy_layout_walks_down_instead() -> None:
    """Direction is computed for the decline exactly as it is for the accept.

    The legacy dialog puts the affirmative first and rests on it, so the two answers swap
    which one needs the arrow — which is the property that makes a *fixed* decline sequence
    as wrong as the fixed Enter this module already removed once.
    """
    assert plan_trust_keys(_LEGACY_PROMPT, _dialog("claude"), accept=False) == ("Down", "Enter")


def test_the_two_answers_differ_only_in_how_far_the_cursor_travels() -> None:
    """Same block, same cursor, same confirm — one row apart, whichever way round it is."""
    for prompt in (_REAL_PROMPT, _LEGACY_PROMPT):
        accept = plan_trust_keys(prompt, _dialog("claude"))
        decline = plan_trust_keys(prompt, _dialog("claude"), accept=False)

        assert accept is not None and decline is not None
        assert accept[-1] == decline[-1] == "Enter"
        assert len(accept) + len(decline) == 3, "one of the two is a bare confirm"
        assert set(accept[:-1]) | set(decline[:-1]) <= {"Up", "Down"}


def test_no_decline_is_planned_when_two_rows_look_like_the_negative() -> None:
    """A screen this cannot choose between is one it presses nothing into.

    The accept path already refuses two affirmatives; the decline needs its own refusal
    because the ambiguity is on a different row, and this one ends a session rather than
    starting it.
    """
    doubled = _REAL_PROMPT.replace(" ❯ No, exit", " ❯ No, exit\n   No, exit")

    assert plan_trust_keys(doubled, _dialog("claude"), accept=False) is None


def test_no_decline_is_planned_for_a_pane_that_is_not_asking() -> None:
    assert plan_trust_keys("Claude Code", _dialog("claude"), accept=False) is None
    assert plan_trust_keys("", _dialog("claude"), accept=False) is None


def test_no_decline_is_planned_when_the_cursor_cannot_be_found() -> None:
    cursorless = _REAL_PROMPT.replace("❯", " ")

    assert plan_trust_keys(cursorless, _dialog("claude"), accept=False) is None


# --- Every agent that asks, read through its own declaration (sub-plan 5, Task 1.3) ---------
#
# The captures are the real ones, read from `tests/fixtures/trust_dialogs/` so the contract
# suite and this one drive the same bytes. The declarations are the *live* ones from each
# vertical, not copies: a test that restated them would pass while the shipped descriptor said
# something else.


def _dialog(profile: str):
    from remote_agents.adapters.agents.registry import provider_descriptors

    return next(
        descriptor.trust_dialog
        for descriptor in provider_descriptors()
        if str(descriptor.profile_id) == profile
    )


@pytest.mark.parametrize("profile", ("codex", "cursor-agent"))
def test_each_asking_agent_is_recognised_by_its_own_declaration(profile: str) -> None:
    assert classify_trust_capture(capture(profile), _dialog(profile)) is TrustState.AWAITING


@pytest.mark.parametrize("profile", ("codex", "cursor-agent"))
def test_each_agents_capture_plans_nothing_under_every_other_agents_dialog(profile: str) -> None:
    """The measured cross-check, and the reason `identifies_by` exists at all.

    codex and cursor-agent draw the same question word for word. If one agent's dialog matched
    the other's screen, the keys would be computed from a row layout that is not on it — and
    since the two rest their cursors differently from claude's, the confirming keypress lands
    on the wrong option rather than merely missing.
    """
    subject = capture(profile)
    for other in ("claude", "codex", "cursor-agent"):
        if other == profile:
            continue
        dialog = _dialog(other)
        assert classify_trust_capture(subject, dialog) is not TrustState.AWAITING, (
            f"{other}'s dialog matched {profile}'s real capture"
        )
        assert plan_trust_keys(subject, dialog, accept=True) is None, (
            f"{other}'s dialog planned keys into {profile}'s pane"
        )


@pytest.mark.parametrize(
    ("profile", "accept", "expected"),
    (
        # Both agents rest the cursor on the affirmative, so accepting is a bare confirm and
        # declining is one row down — the mirror of claude's 2.1.263 layout, which is the whole
        # argument for reading the rows instead of fixing the keys.
        ("codex", True, ("Enter",)),
        ("codex", False, ("Down", "Enter")),
        ("cursor-agent", True, ("Enter",)),
        ("cursor-agent", False, ("Down", "Enter")),
    ),
)
def test_the_keys_are_read_off_each_real_capture(profile, accept, expected) -> None:
    assert plan_trust_keys(capture(profile), _dialog(profile), accept=accept) == expected


def test_an_agent_that_never_asks_declares_no_dialog_to_read_it_with() -> None:
    """opencode's absence is the declaration, and there is nothing to parse with (DEC-009)."""
    assert _dialog("opencode") is None


def test_a_doubled_target_row_still_plans_nothing_for_the_new_dialogs() -> None:
    """Fails closed identically, whichever agent's dialog is being read."""
    doubled = capture("codex").replace("2. No, quit", "2. No, quit\n  2. No, quit")
    assert plan_trust_keys(doubled, _dialog("codex"), accept=False) is None


def test_a_row_between_the_two_options_makes_the_count_a_refusal(capfd) -> None:
    """cursor-agent's box has no blank line to delimit its options — so distance is checked.

    `_option_block` bounds the row count to a run of non-blank lines, which isolates the
    options in claude's dialog and in codex's. It does **not** in cursor-agent's: every row of
    that box carries a `│`, so the "block" is the whole box and the two options are adjacent by
    luck rather than by delimiting. A release that put one description row between them would
    leave a unique cursor and a unique target two rows apart — nothing this module's other
    guards would notice — and the count would stop being the number of times the chooser moves.

    So the distance itself is the check: every measured dialog answers 0 or 1 rows from the
    cursor, and anything else is a layout the arithmetic was never measured against. The owner
    answers by hand; nothing is pressed for them. Found by the Stage 1 Tier-1 review.
    """
    perturbed = capture("cursor-agent").replace(
        "  │    [q] Quit",
        "  │    (this workspace was opened from a link)\n  │    [q] Quit",
    )

    assert "[q] Quit" in perturbed, "the fixture no longer has the row this perturbs"
    assert plan_trust_keys(perturbed, _dialog("cursor-agent"), accept=False) is None, (
        "a row inserted between the options was counted as a step the chooser would take"
    )
    # The accept is unaffected: the cursor already rests on it, so no row is crossed at all.
    assert plan_trust_keys(perturbed, _dialog("cursor-agent"), accept=True) == ("Enter",)


def test_one_row_carrying_both_answers_plans_nothing() -> None:
    """*Decline* must never compute *accept*'s keys, and one row can make it do exactly that.

    A side-by-side chooser puts both option strings on one line, so `target` resolves to the
    same index for either answer and the two plans become identical. On codex and cursor-agent
    the cursor rests on the **affirmative**, so a bare confirm there grants the trust the owner
    had just refused — the worst outcome this module can produce, from a press that says no.

    Refused today by all three measured layouts stacking their options vertically, which is a
    coincidence of those captures rather than a guarantee: this module exists because Claude
    Code's layout already changed once. Raised by the Stage 1 adversarial pass.
    """
    side_by_side = (
        "Do you trust the contents of this directory?\n"
        "\n"
        "› [ Yes, continue ]   [ No, quit ]\n"
        "  something else\n"
    )

    assert plan_trust_keys(side_by_side, _dialog("codex"), accept=True) is None
    assert plan_trust_keys(side_by_side, _dialog("codex"), accept=False) is None
