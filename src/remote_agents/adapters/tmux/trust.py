"""Fixed, profile-owned folder-trust interaction and capture classification.

Claude Code asks, on first launch in a directory it has not been trusted for, whether the
owner trusts the folder -- and then blocks on the answer. A managed launch has nobody at the
keyboard, so the readiness marker never arrives, the startup budget expires, and the record
lands in FAILED with no reason attached. From Telegram that reads as "the launch failed",
which is true and useless: the pane is alive and waiting on one keypress.

This module is the same shape as `remote_control.py`, deliberately -- a fixed key sequence
this project owns, plus a classifier over bounded pane capture -- because answering a dialog
in the pane is a thing the project already does and has reviewed. The alternative considered
and rejected was writing `hasTrustDialogAccepted` into `~/.claude.json`: it is another
application's private, undocumented schema, and every running Claude Code instance writes
that file, so a read-modify-write from here can clobber a concurrent update.
"""

from remote_agents.domain.trust import TrustState

# The dialog opens with the selection already resting on "1. Yes, I trust this folder", and
# its own footer says "Enter to confirm". So the answer is Enter alone: sending "1" first
# would be typing a character into a list that is already on the row we want, and in a menu
# that ever reorders, a literal "1" is not more robust than the cursor -- it is differently
# fragile. This is the same bet `REMOTE_CONTROL_DISCONNECT_KEYS` makes with ("Up", "Up",
# "Enter"): menu geometry is part of the profile's contract with the agent it drives.
TRUST_KEYS = ("Enter",)


def classify_trust_capture(capture: str) -> TrustState:
    """Report whether the pane is sitting on the folder-trust question right now.

    Matched on two markers together rather than one, because either alone appears in
    ordinary agent output the moment somebody discusses this feature -- a session reading
    this docstring would otherwise classify itself as awaiting trust. Requiring the prompt
    *and* its affirmative option is what makes a false positive take a deliberate effort.

    AWAITING is a claim about *now*, not about history: the answer clears the dialog, so a
    later capture of the same pane stops matching. That is why the state has no ANSWERED
    member -- there is nothing in a capture that distinguishes "answered a moment ago" from
    "never asked", and inventing a third answer from an absence is the failure DEC-009 names
    for screens and which applies just as well to a classifier.
    """
    asked_at = capture.rfind("Is this a project you created or one you trust?")
    if asked_at < 0:
        return TrustState.UNKNOWN
    if capture.find("Yes, I trust this folder", asked_at) < 0:
        return TrustState.UNKNOWN
    return TrustState.AWAITING
