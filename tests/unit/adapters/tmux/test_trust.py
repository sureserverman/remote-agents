from remote_agents.adapters.tmux.trust import TRUST_KEYS, classify_trust_capture
from remote_agents.domain.trust import TrustState

# The real dialog, as captured from a managed pane whose launch had just failed. Kept whole
# rather than trimmed to the two matched markers: a classifier tuned to a fragment of the
# thing it classifies is only tested against its own assumptions.
#
# One line is not verbatim: the workspace path was replaced with a synthetic one before this
# repository was published, because the original named an unrelated project of the author's.
# The classifier does not read that line -- it matches the question and its affirmative option
# -- so the substitution costs the fixture nothing it was testing.
_REAL_PROMPT = """
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


def test_the_trust_answer_is_fixed() -> None:
    assert TRUST_KEYS == ("Enter",)


def test_the_real_dialog_is_recognised() -> None:
    assert classify_trust_capture(_REAL_PROMPT) is TrustState.AWAITING


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
