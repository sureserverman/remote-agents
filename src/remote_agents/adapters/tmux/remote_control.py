"""Fixed, profile-owned Claude Remote Control interaction and capture classification."""

from enum import StrEnum

REMOTE_CONTROL_ENABLE_KEYS = ("/remote-control", "Enter")
REMOTE_CONTROL_OPEN_MENU_KEYS = ("/remote-control", "Enter")
REMOTE_CONTROL_DISCONNECT_KEYS = ("Up", "Up", "Enter")
REMOTE_CONTROL_DISMISS_MENU_KEYS = ("Escape",)
"""How to put the status menu away without choosing anything from it.

Needed because `/remote-control` does two different things depending on the pane: it enables
a disconnected session, and it *opens this menu* on a connected one. The second is a no-op
the owner did not ask for, and leaving a menu sitting over their work is not a no-op."""

#: The menu's last line, and the row the arrows aim at. Deliberately **never adjacent in this
#: file** -- see `remote_control_menu_is_open` for why a source file that spelled both on one
#: line was itself enough to defeat the first version of this guard.
_MENU_FOOTER = "Esc to continue"

_MENU_ROW = "Disconnect this session"

#: How far above the footer the row may sit. The real menu is a seven-line block and the row
#: is three lines up; the allowance is loose enough to survive Claude adding an entry and
#: tight enough that a footer landing under unrelated prose is not joined to a distant match.
_MENU_ROW_LOOKBACK = 8

#: What a pane prints when `/remote-control` actually *enabled* it, which is the one outcome
#: of those keys that needs no tidying up afterwards. Used by the enable path to decide
#: whether to dismiss -- see `TmuxTerminal.remote_control`, which keys its `Escape` off this
#: string's **absence** rather than off recognising a menu, so that a menu Claude has reworded
#: is still put away.
REMOTE_CONTROL_ENABLED_MARKER = "/remote-control is active \u00b7 Continue here"

#: How far up the screen the banner may be and still be this pane's own answer. Anchored for
#: the reason the menu markers are: this string appears in this module, so a whole-capture
#: test would find it in a pane *displaying* this module and suppress the dismiss below,
#: leaving a real menu open underneath.
_BANNER_LOOKBACK = 12


def remote_control_was_enabled(capture: str) -> bool:
    """Whether this capture is a pane that just answered `/remote-control` by enabling.

    Read from the tail only, and matched on the banner's **full phrase** rather than on its
    first four words. Both of those are the menu predicate's lesson applied here: the short
    form `/remote-control is active` appears in this module -- `classify_remote_control_capture`
    below searches for it -- and within this file's own last twelve lines, so tail-anchoring
    alone did not separate the banner from a pane displaying the code that looks for it.

    **The residual is stated rather than claimed away.** A pane showing a file that quotes the
    whole phrase would still match. That is tolerable *here* and would not be on the menu
    predicate, because of what each one licenses: this decides whether to send `Escape`, whose
    two failure modes are a redundant keystroke at a prompt and a menu left open, while that
    one decides whether to send `Up, Up, Enter`, which submits the owner's last message. A
    guard is allowed to be only as strong as its blast radius demands, provided somebody has
    said which is which.
    """
    lines = [line for line in capture.splitlines() if line.strip()]
    return any(REMOTE_CONTROL_ENABLED_MARKER in line for line in lines[-_BANNER_LOOKBACK:])


def remote_control_menu_is_open(capture: str) -> bool:
    """Whether this capture is showing Claude's Remote Control status menu.

    **This predicate is a safety guard, not a convenience.** `REMOTE_CONTROL_DISCONNECT_KEYS`
    is `Up, Up, Enter`, which selects *Disconnect this session* from the menu -- and at a bare
    prompt is `history, history, submit`. Measured on claude 2.1.269: sent at a pane with no
    menu on it, those three keys submitted the owner's previous message and started a real
    agent turn that began running shell commands.

    **It reads structure, not vocabulary, and that is the whole of the second version.** The
    first asked only whether both marker strings appeared anywhere in the capture -- and both
    of them sat on one line of *this module*, so a Claude pane displaying this very file, or
    the test beside it, or a grep hit, or a review diff, satisfied it. The owner's sessions
    run in this repository. "Turn it off" pressed against such a pane would have found the
    guard content and typed the arrows at a prompt: the original incident, resurrected by the
    fix for it. Found by the Stage 1 gate's second review pass.

    What distinguishes a menu from text *about* a menu is position. The menu replaces Claude's
    input box while it is up, so its footer is the last thing on the screen; a file being
    displayed has that input box printed underneath it. So the footer must be the final
    non-blank line, and the row must be within `_MENU_ROW_LOOKBACK` lines of it.

    Fails closed, and the asymmetry is deliberate: a false negative costs one refused disable
    the owner can retry, and a false positive types into somebody's session.
    """
    lines = capture.splitlines()
    # Only *trailing* blanks are dropped: `capture-pane -p` returns the pane's full height, so
    # an empty bottom half is padding rather than content. Blanks in the middle are screen
    # rows and stay -- filtering them let a row thirty rows above the footer count as "within
    # eight lines", which is the shape a rendered page with blank-line spacing has.
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not lines[-1].rstrip().endswith(_MENU_FOOTER):
        return False
    # The rows *above* the footer, and the footer's own line is excluded rather than merely
    # not searched: one line spelling both markers is the forgery in miniature, and ordinary
    # prose does it readily -- a sentence telling a reader to pick the disconnect row and
    # then press escape spells both in a row. The real menu never does: the row and the
    # footer are different rows of a widget. (This comment cannot give the example, which is
    # the point of the test that forbids it.)
    above = lines[-(_MENU_ROW_LOOKBACK + 1) : -1]
    return any(_MENU_ROW in line for line in above)


class RemoteControlState(StrEnum):
    """Only states that can be verified from bounded managed-pane capture."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


def classify_remote_control_capture(capture: str) -> RemoteControlState:
    """Use Claude's fixed status markers while honoring the latest observed transition.

    `Disconnect this session` is a **menu row**, and reading it as ACTIVE is sound rather than
    lucky: that menu is what `/remote-control` opens on an *already connected* pane, and a
    disconnected one gets an enable instead. So the row's presence is evidence of the
    connection, not merely of a menu.

    **UNKNOWN is the honest answer for a pane that is connected and idle**, and that is a real
    limit rather than an oversight. A launched pane connects by itself and prints nothing;
    only a *transition* leaves one of these strings on screen, and only the menu states the
    standing fact. Reading the standing fact therefore costs opening the menu, which is an
    action, so it is not something a read may do (`TmuxTerminal.remote_control_state`).
    What the surface does with UNKNOWN is propose *on*, and on an already-connected pane that
    proposal opens the menu, is recognised, and is dismissed -- a visible no-op that reports
    the truth, which is the outcome this classifier is allowed to reach on its own.
    """
    active_at = max(
        capture.rfind("/remote-control is active"),
        capture.rfind("Disconnect this session"),
    )
    disconnected_at = capture.rfind("Remote Control disconnected.")
    if disconnected_at > active_at:
        return RemoteControlState.INACTIVE
    if active_at >= 0:
        return RemoteControlState.ACTIVE
    return RemoteControlState.UNKNOWN
