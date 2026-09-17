"""The F-key row: one table, read by the app's bindings and by anything that describes them.

**Why a table and not eleven `Binding` literals.** The footer, the help text and the prose
that documents the console all have to agree on what each key does and where the convention
was borrowed from, and three hand-kept copies of the same eleven rows is three chances to
disagree. So the row is written once, here, as data -- and `RemoteAgentsTui.BINDINGS` is
*built* from it rather than restating it, the way the retired Alt layer was built from the
row-key table it mirrored.

**F11 is absent by construction.** The table is a tuple literal and the tests count eleven
entries and assert `f11` is not among them; it is left to the terminal, where it is commonly
the full-screen toggle and where a binding of ours would be a key the owner loses without
noticing which side took it.

**Textual names these `f1`..`f12`**, and `test_function_keys.py` proves it against the pinned
parser rather than trusting the convention: an xterm-family terminal sends `\\x1bOP` for F1
and `\\x1b[24~` for F12, and both arrive as `Key` events carrying exactly the names this table
is keyed by.
"""

from __future__ import annotations

from typing import NamedTuple

from textual.binding import Binding

from remote_agents.application.session_actions import ACTION_LABELS, FORCE, GRACEFUL


class SessionKey(NamedTuple):
    """One session-shaped F-key, by the name `action_session_key` accepts.

    `row_key` is the letter the sessions pane binds for the same act, and it is the whole of
    how the key inherits the bare letter's bounds: `RemoteAgentsTui.check_action` asks
    `_offers_session_key` about `row_key`, so F8 on the rename box is refused for exactly the
    reason the bare `s` is not offered there (DEC-052, DEC-062).

    `action` is what `perform_row_action` is handed -- the row-action string for inspect and
    rename, the policy constant for the two stops -- and `None` for the detail, which opens a
    session with no action to perform.
    """

    name: str
    row_key: str
    action: str | None


#: The five session-shaped keys. Ordered as the F-keys that carry them are.
SESSION_KEYS: tuple[SessionKey, ...] = (
    SessionKey("inspect", "i", "inspect"),
    SessionKey("detail", "d", None),
    SessionKey("rename", "r", "rename"),
    SessionKey("graceful", "s", GRACEFUL),
    SessionKey("force", "f", FORCE),
)


#: The row letters whose F-key ends a session: F8 (graceful, unconfirmed by DEC-018) and F9
#: (force, behind a modal). Derived from the policy's own label table rather than spelled, so a
#: third lifecycle action given an F-key tomorrow is refused on a text-entry screen the day it
#: appears rather than the day someone remembers.
#:
#: This is what `RemoteAgentsTui._offers_session_key` refuses on a commitment screen, and it is
#: the set the retired Alt layer carried as its own stop set -- narrower by one, because clean up
#: never got an F-key.
SESSION_STOP_KEYS = frozenset(
    entry.row_key for entry in SESSION_KEYS if entry.action in ACTION_LABELS
)


def session_key(name: str) -> SessionKey | None:
    """The session key called *name*, or `None` for a name the vocabulary does not have."""
    for candidate in SESSION_KEYS:
        if candidate.name == name:
            return candidate
    return None


class FunctionKey(NamedTuple):
    """One row of the F-key table.

    `key` is Textual's name for the key; `action` is the action string the binding runs,
    exactly as `Binding` takes it; `label` is what the footer draws beside the key, lower-case
    like every other footer entry on this surface; `borrowed_from` names the convention the
    owner already knows the key by, which is what the help text will say.
    """

    key: str
    action: str
    label: str
    borrowed_from: str


#: The F-key row, owner-validated. F1-F10 and F12; F11 deliberately not here.
FUNCTION_KEYS: tuple[FunctionKey, ...] = (
    FunctionKey("f1", "help", "help", "htop, mc"),
    FunctionKey("f2", "settings", "settings", "htop Setup"),
    FunctionKey("f3", "session_key('inspect')", "inspect", "mc View"),
    FunctionKey("f4", "session_key('detail')", "detail", "mc Edit"),
    FunctionKey("f5", "refresh", "refresh", "browsers, k9s"),
    FunctionKey("f6", "session_key('rename')", "rename", "mc RenMov"),
    FunctionKey("f7", "add_project", "add project", "mc Mkdir"),
    FunctionKey("f8", "session_key('graceful')", "stop", "mc Delete"),
    FunctionKey("f9", "session_key('force')", "force", "htop Kill"),
    FunctionKey("f10", "quit", "quit", "htop, mc"),
    FunctionKey("f12", "projects_home", "projects", "existing root key"),
)


#: The session-shaped F-keys as a pane advertises them: `F3 F4 F6 F8 F9`.
#:
#: Built from the table so the row of keys the owner reads is the row that works. **Keys alone,
#: no words**, which is a width decision and not a taste one: the hint shares one line with the
#: pane's own keys, that line is `text-overflow: ellipsis` rather than wrapped, and the
#: committed baselines go down to 60 columns -- so `F3 inspect · F8 stop · …` would be elided
#: exactly where the stops are. The footer carries the words now, which the Alt layer this
#: replaces could not do: a hidden chord had nowhere else to be explained.
SESSION_KEY_HINT = " ".join(
    entry.key.upper() for entry in FUNCTION_KEYS if entry.action.startswith("session_key(")
)


def function_key_bindings() -> list[Binding]:
    """The table as app bindings: every one `priority=True` and shown in the footer.

    Priority for the reason the Alt layer is: Textual checks priority bindings from the App
    down before the focused widget sees the key, so an F-key acts from inside the projects
    filter or a rename box rather than being swallowed by the `Input`. Whether it *may* act
    there is `check_action`'s answer, not this table's.
    """
    return [
        Binding(entry.key, entry.action, entry.label, priority=True, show=True)
        for entry in FUNCTION_KEYS
    ]
