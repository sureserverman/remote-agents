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
    CONSOLE_WINDOW_FORMAT,
    console_target,
    display_message_args,
    exact_session_target,
    link_window_args,
    list_console_windows_args,
    parse_console_window,
    select_window_args,
    switch_client_args,
    switch_client_console_args,
    unlink_window_args,
    window_session_mark_args,
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


def test_link_window_appends_the_source_session_into_the_console() -> None:
    assert link_window_args(_SESSION) == ("link-window", "-s", _EXACT, "-t", "ra-console:")


def test_window_session_mark_is_window_scoped_on_the_source() -> None:
    assert window_session_mark_args(_SESSION) == (
        "set-option",
        "-w",
        "-t",
        _EXACT,
        "@remote_agents_window_session",
        str(_SESSION),
    )


def test_unlink_names_one_console_window_and_refuses_the_dashboard() -> None:
    assert unlink_window_args(3) == ("unlink-window", "-t", "ra-console:3")
    with pytest.raises(ValueError):
        unlink_window_args(0)
    with pytest.raises(ValueError):
        unlink_window_args(-1)


def test_select_window_reaches_the_dashboard_and_any_tab() -> None:
    assert select_window_args(0) == ("select-window", "-t", "ra-console:0")
    assert select_window_args(2) == ("select-window", "-t", "ra-console:2")
    with pytest.raises(ValueError):
        select_window_args(-1)


def test_switch_client_targets_are_generated_never_free_text() -> None:
    assert switch_client_args(_SESSION) == ("switch-client", "-t", _EXACT)
    assert switch_client_console_args() == ("switch-client", "-t", "ra-console:")


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


def test_console_window_listing_and_decode_round_trip() -> None:
    assert list_console_windows_args() == (
        "list-windows",
        "-t",
        "ra-console:",
        "-F",
        CONSOLE_WINDOW_FORMAT,
    )
    assert parse_console_window(f"4|{_SESSION}") == (4, _SESSION)
    assert parse_console_window("0|") == (0, None)


@pytest.mark.parametrize("line", ["", "x|y", "1", "not-int|01234567-89ab-cdef-0123-456789abcdef"])
def test_console_window_decode_refuses_ambiguity(line: str) -> None:
    with pytest.raises(ValueError):
        parse_console_window(line)
