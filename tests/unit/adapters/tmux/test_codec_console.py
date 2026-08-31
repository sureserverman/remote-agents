"""Console identity and window-operation argv are generated, exact, and closed.

The console session is named outside the managed namespace on purpose: `ra-console`
carries the `ra-` prefix so it lives visibly on our socket, but it can never satisfy
`exact_session_target`, so no lifecycle code path can ever address it as a session.
Every builder here returns the argv *suffix* the gateway composes after its own
socket selector, and each one validates its target the same way `kill-session` does —
through the codec, never through free text.

Argv shapes verified against real tmux 3.4 on a disposable socket (2026-08-18):
bare `link-window -s ra-<uuid>: -t ra-console:` appends at the next free index, a
window-scoped `@remote_agents_window_session` option set on the source window is
readable from the console's `list-windows`, and `unlink-window -t ra-console:<n>`
leaves the home session running.
"""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.codec import (
    CONSOLE_SESSION_NAME,
    console_layout_args,
    console_target,
    console_zoom_args,
    display_message_args,
    exact_session_target,
    switch_client_argv,
)
from remote_agents.domain.models import SessionId

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_EXACT = "ra-01234567-89ab-cdef-0123-456789abcdef:"


def test_the_console_name_is_never_a_managed_session_target() -> None:
    """The one name collision that would matter is impossible by construction."""
    assert CONSOLE_SESSION_NAME == "ra-console"
    with pytest.raises(ValueError):
        exact_session_target(CONSOLE_SESSION_NAME)


def test_console_target_is_the_exact_session_form() -> None:
    assert console_target() == "ra-console:"


def test_the_exec_handoff_switches_to_the_agents_own_session() -> None:
    """The one switch route left, and the caller it serves is not the console.

    `switch_client_args` — the in-server route the console used to reach an agent — went with
    the tab mechanism (Task 2.4). DEC-039 had already recorded why it could not survive the
    swap model: a session target resolves to whatever occupies the vacated window, so it
    lands the owner on the projects surface rather than on the agent. This one runs on a host
    that composed no console at all, where nothing has been exchanged and the session named
    is the agent's own.
    """
    argv = switch_client_argv(_SESSION)

    assert argv[:3] == ("tmux", "-L", "remote-agents")
    assert argv[3:] == ("switch-client", "-t", _EXACT)


def test_display_message_carries_one_status_line_literally() -> None:
    """`-l` pins literal rendering: without it tmux format-expands the message, and
    `#(shell-command)` in FORMATS executes — so the flag is the difference between a
    status flash and an arbitrary-command sink (verified against tmux 3.4, 2026-08-18)."""
    assert display_message_args("agent finished: #(id)") == (
        "display-message",
        "-l",
        "--",
        "agent finished: #(id)",
    )
    # `--` fences the one caller-controlled string from the option parser: a message
    # beginning with `-` must arrive as text, never be consumed as a display-message flag.
    assert display_message_args("-a looks like a flag")[-2:] == ("--", "-a looks like a flag")
    with pytest.raises(ValueError):
        display_message_args("")
    with pytest.raises(ValueError):
        display_message_args("two\nlines")


def test_the_zoom_probe_asks_whether_anything_is_hiding_the_feed() -> None:
    """What replaced the current-window read once the console had exactly one window.

    That read was the tab model's proxy for "the owner is looking at the dashboard". With one
    window it answers 0 forever, so the status flash it guarded could never fire again — a
    rule whose premise had been deleted. The question now is whether a zoomed pane is hiding
    the feed, and the format is this module's own fixed text.
    """
    argv = console_zoom_args()

    assert argv[:4] == ("display-message", "-p", "-t", console_target())
    assert argv[4] == "#{window_zoomed_flag}|#{pane_id}"


def test_the_layout_resizes_the_right_column_in_the_order_it_is_given() -> None:
    """One resize per named pane, after the layout that flattened them, top pane first.

    `select-layout main-vertical` divides the right column *evenly*, so every pane in it that
    is not meant to be an even share has to be resized back. With the column three panes deep
    that is more than one resize, and they are not commutative: probed on tmux 3.4 at 183x44,
    a resize takes its rows from the panes below the one named, and a resize aimed at the
    **bottom** pane works against the pane above it instead -- so naming only the feed left the
    column at 14/15/13 and the sessions list two rows shorter than the pane beside it.
    """
    argv = console_layout_args(60, (("%1", 46), ("%2", 33)))

    assert argv[0][:2] == ("set-window-option", "-t")
    assert argv[1][-1] == "main-vertical"
    assert argv[2:] == (
        ("resize-pane", "-t", "%1", "-y", "46%"),
        ("resize-pane", "-t", "%2", "-y", "33%"),
    )


def test_a_layout_column_takes_percentages_and_pane_ids_or_nothing() -> None:
    with pytest.raises(ValueError):
        console_layout_args(60, (("%1", 0),))
    with pytest.raises(ValueError):
        console_layout_args(60, (("%1", 100),))
    with pytest.raises(ValueError):
        console_layout_args(60, (("ra-console:", 41),))
    assert console_layout_args(60, ())[2:] == (), "a column with nothing named resizes nothing"
