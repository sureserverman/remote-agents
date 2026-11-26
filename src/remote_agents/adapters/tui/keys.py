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

from remote_agents.application.session_actions import FORCE, GRACEFUL
from remote_agents.ports.console import StatusBarKey

# Console stops resolve the displayed session or the published selection.
SESSION_STOP_ACTIONS = frozenset({GRACEFUL, FORCE})


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
    console's tmux status row supplies its function-key legend.

    `short` is the one word the console's compact status bar draws (DEC-105), where `label`
    would not fit: `1help 2setup 3view …`. Three are the borrowed sources' own words
    (DEC-095) -- htop's Setup and Kill, mc's View; the rest are the label, shortened.
    """

    key: str
    action: str
    label: str
    borrowed_from: str
    footer: bool
    short: str


#: The F-key row, owner-validated. F1-F10 and F12; F11 deliberately not here.
FUNCTION_KEYS: tuple[FunctionKey, ...] = (
    FunctionKey("f1", "help", "help", "htop, mc", footer=True, short="help"),
    FunctionKey("f2", "settings", "settings", "htop Setup", footer=False, short="setup"),
    FunctionKey("f3", "toggle_panes", "panes", "console layout", footer=False, short="panes"),
    FunctionKey(
        "f4",
        "waiting_sessions",
        "waiting",
        "console navigation",
        footer=False,
        short="waiting",
    ),
    FunctionKey("f5", "refresh", "refresh", "browsers, k9s", footer=False, short="refresh"),
    FunctionKey("f6", "fresh_session", "fresh", "session lifecycle", footer=False, short="fresh"),
    FunctionKey("f7", "add_project", "add project", "mc Mkdir", footer=True, short="addproj"),
    FunctionKey("f8", "session_key('graceful')", "stop", "mc Delete", footer=False, short="stop"),
    FunctionKey(
        "f9", "session_key('force')", "force stop", "htop Kill", footer=False, short="kill"
    ),
    FunctionKey("f10", "quit", "quit", "htop, mc", footer=True, short="close"),
    FunctionKey(
        "f12", "projects_home", "projects", "existing root key", footer=False, short="projects"
    ),
)


#: F11 as the console's status bar draws it: dim, labelled, and **bound nowhere** (DEC-093).
#:
#: Drawn so the owner reading `10 close console  12 projects` does not wonder where 11 went;
#: kept out of `FUNCTION_KEYS` so no binding list built from that table can ever reach it --
#: which is the construction the module docstring argues for, unchanged. `action` is empty
#: because there is nothing to run. The compact bar drops it first.
TERMINAL_KEY = FunctionKey("f11", "", "terminal", "the terminal", footer=False, short="terminal")


#: Every key the console's status bar draws, in key order: the bound table plus F11.
BAR_KEYS: tuple[FunctionKey, ...] = (*FUNCTION_KEYS[:10], TERMINAL_KEY, *FUNCTION_KEYS[10:])


#: What a **console** surface pane calls each footer entry whose meaning depends on its host,
#: by key. Derived from the table, so the rule is a rule rather than a fact about one key.
#:
#: **This replaces a set that kept those entries out of the console footer entirely, and
#: the reason it could be replaced is the whole of BL-097.** That set existed because pressing
#: `quit` in a console surface pane destroyed the pane the owner was reading: the panes carry
#: no `remain-on-exit`, so the process ending closed the pane and tmux reflowed the layout over
#: the gap with nothing to rebuild it. A footer entry reading `quit` was therefore advertising
#: something false, and dropping the drawing was the cheap honest half of a fix.
#:
#: Under DEC-096 the key means what the owner meant by it — the whole console goes and the
#: shell comes back — so it is advertising a true thing again and may be drawn. What it may
#: **not** do is keep the bare terminal's word: off a console `quit` costs the owner one
#: process they started on purpose, and in a console pane one press now closes four panes.
#: `close console` is that difference, in the owner's vocabulary rather than the table's.
#:
#: **Only the words change.** The key, the action and the binding are identical in both
#: hostings — DEC-093 keeps every F-key bound and DEC-095 keeps `F10` meaning what htop and mc
#: mean by it, and neither is touched by relabelling a *drawing*. No count is written here on
#: purpose: `ConsolePaneSlot`'s own docstring records that prose restating the size of the
#: thing it describes is a second declaration nothing keeps true.
CONSOLE_FOOTER_LABELS: dict[str, str] = {
    entry.key: "close console" for entry in FUNCTION_KEYS if entry.action == "quit"
}


#: The keys, besides the stops, that a text-entry screen refuses: F2 (settings) and F7 (add
#: project) would each open a screen over a half-typed name. The console bar dims them there.
_REFUSED_WHILE_TYPING = frozenset({"settings", "add_project", "fresh_session", "waiting_sessions"})


def status_bar_keys() -> tuple[StatusBarKey, ...]:
    """`BAR_KEYS` as the console's status bar draws them (DEC-105), one table with the bindings.

    The bar only exists under console hosting, so the console's own labels apply
    (`CONSOLE_FOOTER_LABELS`: F10 reads `close console`, DEC-096). Which keys dim is derived
    here from what each one runs rather than listed for the bar: a session key needs a
    selection, and a stop key -- `SESSION_STOP_ACTIONS`, the set `_offers_session_key` refuses on
    a commitment screen -- is refused while typing.
    """
    stop_commands = {f"session_key('{action}')" for action in SESSION_STOP_ACTIONS}
    keys = []
    for entry in BAR_KEYS:
        stop = entry.action in stop_commands
        keys.append(
            StatusBarKey(
                number=int(entry.key[1:]),
                label=CONSOLE_FOOTER_LABELS.get(entry.key, entry.label),
                short=entry.short,
                needs_selection=stop,
                refused_while_typing=entry.action in _REFUSED_WHILE_TYPING or stop,
                bound=entry is not TERMINAL_KEY,
            )
        )
    return tuple(keys)


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
