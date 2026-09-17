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
    exactly as `Binding` takes it; `label` is the words drawn beside the key, lower-case like
    every other entry on this surface; `borrowed_from` names the convention the owner already
    knows the key by, which is what the help text will say.

    **`footer` is not about whether the key exists -- every one of them is bound, on every
    position that answers for it.** It is about a fixed-width line that cannot hold eleven
    entries: `Footer` draws `active_bindings` in order and *clips*, so a row that advertises
    more than it can draw tells the owner about keys it then hides mid-word. Measured on
    `InspectScreen` at 80 columns, which carries two bindings of its own: five app entries fit
    and six do not.

    So the footer carries the acts with no other advertisement on screen, and F1's help panel
    is the complete list -- `BindingsTable` renders `active_bindings` without filtering on
    `show`, so every key here is in it, which is what the key is borrowed from htop *for*. The
    five session-shaped keys are left out because the pane's own hint row names them, and F2,
    F5 and F12 because `,`, `ctrl+r` and the palette already do.
    """

    key: str
    action: str
    label: str
    borrowed_from: str
    footer: bool


#: The F-key row, owner-validated. F1-F10 and F12; F11 deliberately not here.
FUNCTION_KEYS: tuple[FunctionKey, ...] = (
    FunctionKey("f1", "help", "help", "htop, mc", footer=True),
    FunctionKey("f2", "settings", "settings", "htop Setup", footer=False),
    FunctionKey("f3", "session_key('inspect')", "inspect", "mc View", footer=False),
    FunctionKey("f4", "session_key('detail')", "detail", "mc Edit", footer=False),
    FunctionKey("f5", "refresh", "refresh", "browsers, k9s", footer=False),
    FunctionKey("f6", "session_key('rename')", "rename", "mc RenMov", footer=False),
    FunctionKey("f7", "add_project", "add project", "mc Mkdir", footer=True),
    FunctionKey("f8", "session_key('graceful')", "stop", "mc Delete", footer=False),
    FunctionKey("f9", "session_key('force')", "force", "htop Kill", footer=False),
    FunctionKey("f10", "quit", "quit", "htop, mc", footer=True),
    FunctionKey("f12", "projects_home", "projects", "existing root key", footer=False),
)


#: The footer entries a **console** surface pane withholds, by key. Derived from the table, so
#: the rule is a rule rather than a fact about one key.
#:
#: **Why a key is withheld here and nowhere else.** `footer` above answers "does this entry fit
#: on a clipping line"; this answers a different question -- "does pressing this entry cost the
#: owner the thing they are looking at". Off a console, `quit` leaves the app and the terminal
#: comes back. In a console surface pane it ends *that pane's* process, and the panes carry no
#: `remain-on-exit`: tmux closes the pane and reflows the layout over the gap, so a footer entry
#: the owner read as "leave" silently destroys the pane they were reading (BL-097, hit on
#: 2026-09-17; the console ran a pane short for twenty minutes). No count is written here on
#: purpose -- `ConsolePaneSlot`'s own docstring records that prose restating the size of the
#: thing it describes is a second declaration nothing keeps true, and it said "three" while
#: carrying four members for exactly that reason.
#:
#: **De-advertisement, not removal.** The key stays bound, it still quits, and F1's panel still
#: lists it -- `BindingsTable` renders `active_bindings` without filtering on `show`, which is
#: what the key was borrowed from htop for. DEC-093 keeps every F-key bound and DEC-095 keeps
#: F10 meaning what htop and mc mean by it; neither is touched by withholding a *drawing*.
#:
#: The self-healing alternative -- a surviving pane noticing the gap and rebuilding the layout
#: -- is the one BL-039 blocks: that rebuild path is what put the sessions pane into Textual
#: 8.2.8's `Screen._refresh_layout` loop at 100% CPU. So the cheap, honest half ships and the
#: pane the owner closes stays closed.
CONSOLE_WITHHELD_FROM_FOOTER: frozenset[str] = frozenset(
    entry.key for entry in FUNCTION_KEYS if entry.action == "quit"
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
    """The table as app bindings: every one `priority=True`, shown where the table says.

    Priority for the reason the retired Alt layer was: Textual checks priority bindings from
    the App down before the focused widget sees the key, so an F-key acts from inside the
    projects filter or a rename box rather than being swallowed by the `Input`. Whether it
    *may* act there is `check_action`'s answer, not this table's; whether the footer draws it
    is `footer`'s, argued at `FunctionKey`.
    """
    return [
        Binding(entry.key, entry.action, entry.label, priority=True, show=entry.footer)
        for entry in FUNCTION_KEYS
    ]
