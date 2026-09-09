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

**The one thing this module no longer assumes is where the cursor is resting.** It used to
send a bare Enter, on the stated argument that the dialog opens on "1. Yes, I trust this
folder" with a footer reading "Enter to confirm". That was true of the dialog it was written
against and is false of Claude Code 2.1.263, which drops the numbering, puts "No, exit"
first, and rests the cursor there. A bare Enter into that dialog picks "No, exit": the agent
exits, the managed pane is left holding nothing, and the owner who pressed *Trust* watches
the session die. So the keys are now read off the same capture that classified the dialog --
see `plan_trust_keys`.
"""

from remote_agents.domain.trust import TrustState

#: The selection marker Claude Code's chooser draws on the row the cursor rests on (U+276F).
TRUST_CURSOR = "❯"

#: The question, and the option this project is willing to choose. Matched as substrings so
#: the numbering the dialog has carried in some versions ("1. Yes, I trust this folder") and
#: not in others is not part of the contract.
TRUST_QUESTION = "Is this a project you created or one you trust?"
TRUST_AFFIRMATIVE = "Yes, I trust this folder"

#: The option that answers *no* and takes the agent down with it.
#:
#: Recorded from Claude Code 2.1.263 and **not re-measured since**: the host this was last
#: worked on sets `permissions.defaultMode: "auto"` in `~/.claude/settings.json`, under which
#: 2.1.265 raises no folder-trust dialog at all, so fifteen launches produced nothing to read
#: (`docs/acceptance-2026-09-08-untrusted-launch.md`, section 1). It is matched as a substring
#: for the same reason the affirmative is -- the numbering has come and gone between versions
#: -- and `plan_trust_keys` fails closed when the row is missing or doubled, so a wording that
#: has moved on costs a refusal to press anything rather than a wrong key.
TRUST_NEGATIVE = "No, exit"

#: What confirms the row the cursor is on. Only ever sent after the movement `plan_trust_keys`
#: computed, and never on its own.
TRUST_CONFIRM_KEY = "Enter"


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
    asked_at = capture.rfind(TRUST_QUESTION)
    if asked_at < 0:
        return TrustState.UNKNOWN
    if capture.find(TRUST_AFFIRMATIVE, asked_at) < 0:
        return TrustState.UNKNOWN
    return TrustState.AWAITING


def plan_trust_keys(capture: str, *, accept: bool = True) -> tuple[str, ...] | None:
    """The exact keys that move this dialog's cursor onto an answer and confirm, or None.

    `accept` picks which answer. **Both answers are planned by one rule off one capture**,
    which is the point rather than a convenience: a fixed decline sequence would be wrong in
    exactly the way the fixed Enter was, and for the same reason -- the two options have
    already swapped places once between Claude Code versions, so whichever answer is being
    given, the row it lives on has to be read rather than assumed. Reading both off the same
    capture also means the two can never disagree about where the cursor is.

    Declining is offered for a narrower reason than accepting and is not its mirror image:
    saying *yes* commits the owner's trust to a directory, while saying *no* ends a session
    that has not started. `application/session_actions` is where that asymmetry is decided;
    this function only draws the keys.

    **Why this is computed rather than fixed.** The dialog's geometry is part of the
    profile's contract with the agent it drives, and Claude Code has now changed it once:
    the option that was first, numbered and pre-selected is now second, unnumbered, and the
    cursor rests on "No, exit" instead. A constant key sequence cannot be right across both,
    and a constant that is wrong here does not misfire harmlessly -- it answers *no* and
    takes the agent down. So the keys are derived from the capture that classified the
    dialog, which is the only thing on the host that actually knows where the cursor is.

    **Fails closed, and that is the point.** Every way of not finding the answer returns
    `None`, and `TmuxRuntime.answer_trust` declines rather than sending anything. The old
    failure mode was sending a confirming keypress on an assumption; refusing to press
    anything when the screen is not the one we can read leaves the pane exactly as it was,
    and the owner can still answer it by hand.

    The cursor and the affirmative option must sit in the *same* contiguous block of
    non-blank lines. That is what makes the returned count a number of rows the chooser will
    actually travel: two markers separated by a blank line are not two rows of one list, and
    counting between them would be arithmetic over a layout nobody has seen.
    """
    if classify_trust_capture(capture) is not TrustState.AWAITING:
        return None
    lines = capture[capture.rfind(TRUST_QUESTION) :].splitlines()
    block = _option_block(lines)
    if block is None:
        return None
    cursor = _sole_index(block, TRUST_CURSOR)
    # Anchored on the affirmative even when declining, because `_option_block` is: a capture
    # whose "Yes" row cannot be found is one whose option list this cannot delimit, and
    # counting rows in a block it could not delimit is the arithmetic-over-an-unseen-layout
    # this module already refuses.
    target = _sole_index(block, TRUST_AFFIRMATIVE if accept else TRUST_NEGATIVE)
    if cursor is None or target is None:
        return None
    steps = target - cursor
    movement = ("Down",) * steps if steps > 0 else ("Up",) * -steps
    return (*movement, TRUST_CONFIRM_KEY)


def _option_block(lines: list[str]) -> list[str] | None:
    """The run of non-blank lines holding the affirmative option, or None if it is alone.

    The chooser draws its options as adjacent rows, so the run containing "Yes" is the list
    the cursor moves within. Anchored on the affirmative rather than on the cursor because
    the affirmative is the row this project is trying to reach; a capture whose cursor has
    scrolled out of the block is one this cannot count across, and `plan_trust_keys` then
    declines.
    """
    for index, line in enumerate(lines):
        if TRUST_AFFIRMATIVE not in line:
            continue
        start = index
        while start > 0 and lines[start - 1].strip():
            start -= 1
        end = index
        while end + 1 < len(lines) and lines[end + 1].strip():
            end += 1
        return lines[start : end + 1]
    return None


def _sole_index(block: list[str], marker: str) -> int | None:
    """Where `marker` sits in the option block, or None unless exactly one row carries it.

    Two rows carrying the cursor is not a dialog this can read, and two rows offering to
    trust the folder is not one it should guess between. Both are `None`, which is a refusal
    to press a key rather than a wrong key pressed.
    """
    hits = [index for index, line in enumerate(block) if marker in line]
    return hits[0] if len(hits) == 1 else None
