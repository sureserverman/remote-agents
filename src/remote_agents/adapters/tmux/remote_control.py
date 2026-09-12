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

#: Both must be present for the status menu to be considered on screen. Two markers rather
#: than one because the predicate below gates a keystroke that is destructive when it misses
#: (see `remote_control_menu_is_open`), and the cost of being wrong is asymmetric: an extra
#: marker can only make this refuse, never make it fire at the wrong moment.
_MENU_MARKERS = ("Disconnect this session", "Esc to continue")


def remote_control_menu_is_open(capture: str) -> bool:
    """Whether this capture is showing Claude's Remote Control status menu.

    **This predicate is a safety guard, not a convenience.** `REMOTE_CONTROL_DISCONNECT_KEYS`
    is `Up, Up, Enter`, which selects *Disconnect this session* from the menu -- and at a bare
    prompt is `history, history, submit`. Measured on claude 2.1.269: sent at a pane with no
    menu on it, those three keys submitted the owner's previous message and started a real
    agent turn that began running shell commands. The disable path used to send them after a
    fixed sleep, on the assumption that asking for the menu had produced one.

    Fails closed, and the asymmetry is deliberate: a false negative costs one refused disable
    that the owner can retry, and a false positive types into somebody's session.
    """
    return all(marker in capture for marker in _MENU_MARKERS)


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
