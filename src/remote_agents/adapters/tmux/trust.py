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

import logging

from remote_agents.domain.trust import TrustState
from remote_agents.ports.provider_descriptor import TrustDialog

_LOG = logging.getLogger(__name__)

#: **The strings moved out of this module on 2026-09-09, and the move is the feature.**
#:
#: Claude's question, options and cursor glyph were module constants here — which is what
#: confined answering the trust question to Claude, since a second agent's dialog had nowhere
#: to be declared. They now live on `claude`'s `ProviderDescriptor.trust_dialog`, beside
#: codex's and cursor-agent's, and every function here reads the dialog it is handed
#: (DEC-070: a value that discriminates on provider identity belongs to the vertical).
#:
#: What did not change is the failing-closed: a capture whose identifier is absent, whose
#: cursor cannot be found, or whose target row is doubled plans nothing, whichever agent's
#: declaration is being read. A wording that has moved on costs a refusal to press anything
#: rather than a wrong key pressed — which matters more now than it did with one agent, since
#: the three dialogs disagree about which row the cursor starts on.

#: What confirms the row the cursor is on. Only ever sent after the movement `plan_trust_keys`
#: computed, and never on its own.
TRUST_CONFIRM_KEY = "Enter"


def classify_trust_capture(capture: str, dialog: TrustDialog) -> TrustState:
    """Report whether the pane is sitting on **this agent's** folder-trust question right now.

    Matched on three markers together rather than one, because any of them alone appears in
    ordinary agent output the moment somebody discusses this feature -- a session reading
    this docstring would otherwise classify itself as awaiting trust. Requiring the prompt
    *and* its affirmative option is what makes a false positive take a deliberate effort.

    **The third marker is `identifies_by`, and it is what makes the answer about one agent.**
    `codex` and `cursor-agent` draw *"Do you trust the contents of this directory?"* word for
    word (measured; `docs/acceptance-2026-09-09-trust-dialogs.md`), so a classifier keyed on
    the question alone says AWAITING for the wrong agent's screen -- and `plan_trust_keys`
    then counts rows on a layout that is not there. The two rest their cursors on the
    affirmative and claude's rests on the negative, so that miscount does not merely miss: it
    presses the option the owner did not choose. `identifies_by` is looked for across the whole
    capture rather than after the question, because cursor-agent draws its identifier *above*
    the question, inside the same box.

    AWAITING is a claim about *now*, not about history: the answer clears the dialog, so a
    later capture of the same pane stops matching. That is why the state has no ANSWERED
    member -- there is nothing in a capture that distinguishes "answered a moment ago" from
    "never asked", and inventing a third answer from an absence is the failure DEC-009 names
    for screens and which applies just as well to a classifier.
    """
    if dialog.identifies_by not in capture:
        return TrustState.UNKNOWN
    asked_at = capture.rfind(dialog.question)
    if asked_at < 0:
        return TrustState.UNKNOWN
    if capture.find(dialog.affirmative, asked_at) < 0:
        return TrustState.UNKNOWN
    return TrustState.AWAITING


def plan_trust_keys(
    capture: str, dialog: TrustDialog, *, accept: bool = True
) -> tuple[str, ...] | None:
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
    if classify_trust_capture(capture, dialog) is not TrustState.AWAITING:
        return None
    lines = capture[capture.rfind(dialog.question) :].splitlines()
    block = _option_block(lines, dialog.affirmative)
    if block is None:
        return None
    cursor = _sole_index(block, dialog.cursor)
    # **The two answers must be two rows.** Nothing here required that until the Stage 1
    # adversarial pass asked what happens when one row carries both option strings -- a
    # side-by-side chooser (`› [ Yes, continue ]  [ No, quit ]`), or any screen where the two
    # phrases land on one line. `target` is then the same index for both answers, so *decline*
    # computes *accept*'s keys: on codex and cursor-agent, whose cursor rests on the
    # affirmative, pressing "Don't trust" would confirm the trust the owner had just refused.
    # Refused today only because all three measured dialogs stack their options vertically,
    # which is a coincidence of the layouts and not a check -- and this module exists because a
    # layout already changed once.
    if _sole_index(block, dialog.affirmative) == _sole_index(block, dialog.negative):
        _LOG.debug("both answers resolve to one row; nothing is pressed")
        return None
    # Anchored on the affirmative even when declining, because `_option_block` is: a capture
    # whose "Yes" row cannot be found is one whose option list this cannot delimit, and
    # counting rows in a block it could not delimit is the arithmetic-over-an-unseen-layout
    # this module already refuses.
    target = _sole_index(block, dialog.affirmative if accept else dialog.negative)
    if cursor is None or target is None:
        return None
    steps = target - cursor
    if abs(steps) > 1:
        # **Row distance is only a proxy for *selection* distance, and the proxy holds exactly
        # as far as the rows between the two are themselves selectable.** Every dialog this
        # project has measured is a two-option chooser whose options are adjacent rows, so the
        # answer is always 0 or 1 steps away; a larger number does not mean "further", it means
        # this is not one of the layouts the arithmetic was measured against, and a
        # non-selectable row caught between the cursor and the target would make the count
        # wrong rather than merely unfamiliar.
        #
        # It matters more since the dialogs became three. `_option_block` bounds the count to a
        # run of non-blank lines, which isolates the options in claude's and codex's dialogs --
        # and does **not** in cursor-agent's, whose box draws `|` on every row, so the "block"
        # is the whole box and the two options are adjacent by luck rather than by delimiting.
        # Refusing here is what makes that luck unnecessary: a cursor-agent release that puts a
        # description row between its two options costs the owner a keypress they make by hand,
        # not a wrong option pressed for them. Found by the Stage 1 Tier-1 review.
        _LOG.debug(
            "the trust dialog's answer is %d rows from the cursor; only an adjacent two-option "
            "chooser has been measured, so nothing is pressed",
            steps,
        )
        return None
    movement = ("Down",) * steps if steps > 0 else ("Up",) * -steps
    return (*movement, TRUST_CONFIRM_KEY)


def _option_block(lines: list[str], affirmative: str) -> list[str] | None:
    """The run of non-blank lines holding the affirmative option, or None if it is alone.

    The chooser draws its options as adjacent rows, so the run containing "Yes" is the list
    the cursor moves within. Anchored on the affirmative rather than on the cursor because
    the affirmative is the row this project is trying to reach; a capture whose cursor has
    scrolled out of the block is one this cannot count across, and `plan_trust_keys` then
    declines.
    """
    for index, line in enumerate(lines):
        if affirmative not in line:
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
