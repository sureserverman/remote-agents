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
    CONSOLE_SLOT_OPTION,
    SELECTED_SESSION_OPTION,
    console_binding_args,
    console_layout_args,
    console_target,
    console_zoom_args,
    decode_selection,
    display_message_args,
    exact_session_target,
    publish_selection_args,
    read_selection_args,
    switch_client_argv,
)
from remote_agents.domain.models import SessionId
from remote_agents.ports.console import ConsoleBindingAction, ConsoleKeyTable

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
    argv = console_layout_args(60, (("%1", 53), ("%2", 35)))

    assert argv[0][:2] == ("set-window-option", "-t")
    assert argv[1][-1] == "main-vertical"
    assert argv[2:] == (
        ("resize-pane", "-t", "%1", "-y", "53%"),
        ("resize-pane", "-t", "%2", "-y", "35%"),
    )


def test_a_layout_column_takes_percentages_and_pane_ids_or_nothing() -> None:
    with pytest.raises(ValueError):
        console_layout_args(60, (("%1", 0),))
    with pytest.raises(ValueError):
        console_layout_args(60, (("%1", 100),))
    with pytest.raises(ValueError):
        console_layout_args(60, (("ra-console:", 41),))
    assert console_layout_args(60, ())[2:] == (), "a column with nothing named resizes nothing"


def test_the_selection_is_published_at_session_scope_and_read_back() -> None:
    """The console's selected session is console state, not pane identity — hence `-t`, not `-p`.

    DEC-038 puts identity on the pane, because a mark that stays behind while the pane travels
    describes whatever swapped in. That reasoning is about *identity*, and it does not apply
    here: a selection is one fact about the console as a whole — which session its sessions
    pane has highlighted — and every pane process must read the same answer. Session scope is
    what makes one writer visible to three readers; pane scope would give each pane its own
    private copy of a shared fact, which is the defect DEC-038 describes running the other way.

    The target is the console session by name, never a managed one: `ra-console` cannot satisfy
    `exact_session_target`, so this can never address a session as though it were an agent's.
    """
    session_id = SessionId.new()

    assert publish_selection_args(session_id) == (
        "set-option",
        "-t",
        console_target(),
        SELECTED_SESSION_OPTION,
        str(session_id),
    )
    assert read_selection_args() == (
        "show-options",
        "-qv",
        "-t",
        console_target(),
        SELECTED_SESSION_OPTION,
    )


def test_publishing_no_selection_writes_an_empty_value() -> None:
    """Resting on nothing is a value, and it has to be written rather than left behind.

    The sessions pane clears the cursor whenever the highlighted row leaves the list
    (DEC-052, DEC-062). If that published nothing at all, the option would still name the row
    that has gone and a chord pressed in another pane would act on it — the stale publication
    the whole design has to avoid. Empty is the spelling, because `show-options -qv` returns
    the empty string for an option that was never set, so "cleared" and "never written" decode
    to the same thing by construction rather than by agreement.
    """
    assert publish_selection_args(None) == (
        "set-option",
        "-t",
        console_target(),
        SELECTED_SESSION_OPTION,
        "",
    )


def test_a_published_selection_decodes_back_to_the_session_it_named() -> None:
    session_id = SessionId.new()

    assert decode_selection(f"{session_id}\n") == session_id


@pytest.mark.parametrize("raw", ["", "   ", "\n", "not-a-uuid", "\x00", "ra-console"])
def test_anything_that_is_not_a_session_decodes_to_no_selection(raw: str) -> None:
    """Including the empty string, which is what an unset option reads back as.

    A reader must never turn a malformed value into a session id: the chord layer acts on
    whatever this returns, and `s` and `c` end a session without asking. Refusing is the only
    safe answer for a value this process did not write.
    """
    assert decode_selection(raw) is None


def test_a_prefix_binding_forwards_the_key_to_the_pane_carrying_the_sessions_mark() -> None:
    """The argv one forwarding chord installs, and the two escapes it depends on.

    **`-T prefix`, never `-n`.** That is the difference between a key that costs every agent on
    this server nothing and eight keys that cost them everything (DEC-041).

    **The pane is resolved at press time, by tmux, from the slot mark.** A pane id captured at
    install would forward the key into whatever holds that number once the pane is rebuilt; the
    mark travels with the pane and outlives it (DEC-038). No *pane id* of ours has to still be
    right when the key is pressed — one *name* does, and the guard asserted below is why.

    The `##{...}` is not a typo and is the whole reason this is asserted at argv level:
    `run-shell` expands its string as a tmux **format** before `/bin/sh` sees it, so the doubling
    is what carries the lookup's own `#{...}` through to the *inner* tmux. Measured on tmux 3.4
    together with the delivery itself — the emitted argv resolves the marked pane and the key
    arrives in it.
    """
    argv = console_binding_args(
        "M-s", ConsoleBindingAction.FORWARD_TO_SESSIONS, table=ConsoleKeyTable.PREFIX
    )

    assert argv[:4] == ("bind-key", "-T", "prefix", "M-s"), (
        f"a forwarding chord must go in the prefix table, not the root one: {argv}"
    )
    assert argv[4] == "run-shell"
    script = argv[5]
    assert "-n" not in argv, "a forwarding chord in the root table would cost every agent a key"
    assert "##{pane_id}" in script, "the inner tmux will not see a pane-id format to expand"
    assert f"##{{{CONSOLE_SLOT_OPTION}}}" in script, "the lookup does not read the slot mark"
    assert "sessions" in script, "the lookup does not name the sessions slot"
    # The key the binding forwards must be the key it is bound to. Asserted on the `send-keys`
    # clause rather than on the string's tail, which carries shlex's own closing quote.
    assert 'send-keys -t "$pane" M-s' in script, f"the key forwarded is not the one bound: {script}"

    # **The guard, pinned here because this is the suite CI actually runs.** `tests/live` is not
    # in `ci.yml`'s list and is skipped locally without an opt-in flag, so before this assertion
    # the whole protection could be deleted and every gating test stayed green.
    #
    # What it protects: a tmux key table belongs to the *server*, and managed agents attach on
    # that same server, so without this clause the chord fired from any client on the socket --
    # an owner in a plain `remote-agents attach` sending an unconfirmed stop (DEC-018) to a row
    # they could not see. Reproduced on a real server before it was closed; DEC-073(3).
    #
    # **The whole clause, not its parts.** Two substring assertions were the first attempt and
    # both survived an inverted guard: `!= "ra-console"` still contains `= "ra-console"`, and
    # `|| true` in place of `|| exit 0` was asserted by nothing at all. Either mutant keeps CI
    # green while the chord fires from *every* client except the console, or from all of them —
    # which is the DEC-018 hole this fence exists for, reopened by a test that only checks the
    # fence is mentioned.
    guard = (
        f'test "$(tmux display-message -p "##{{client_session}}")" = "{CONSOLE_SESSION_NAME}" '
        f"|| exit 0;"
    )
    assert guard in script, (
        "the forwarding chord does not refuse a client attached to anything but the console, "
        f"so it fires from any client on this server (DEC-073(3)): {script}"
    )


def test_a_forwarding_chord_is_refused_in_the_root_table() -> None:
    """Refused where it is built, rather than left to a caller's care.

    The eight chords are affordable *because* they are prefix keys. A caller that asked for one
    in the root table would be spending eight keys from a budget of one — silently, since the
    argv is otherwise identical and every test of the chord layer would still pass.
    """
    with pytest.raises(ValueError, match="prefix table"):
        console_binding_args(
            "M-s", ConsoleBindingAction.FORWARD_TO_SESSIONS, table=ConsoleKeyTable.ROOT
        )

    # A *third* table needs no runtime check any more: `ConsoleKeyTable` is a closed set, so
    # there is no third value to pass. That check existed while `table` was a bare string and
    # went away with the string — the enum earning its place, not a weakening. The root binding
    # still builds, unaffected by any of this.
    assert console_binding_args(
        "F12", ConsoleBindingAction.SHOW_PROJECTS, ("true",), table=ConsoleKeyTable.ROOT
    )[:3] == ("bind-key", "-n", "F12")


def test_a_forwarding_chord_builds_its_own_command() -> None:
    """It takes no command, because the one it needs is derived from the key it is bound to.

    `SHOW_PROJECTS` runs *our program* and so must be handed it; this runs tmux against tmux and
    can build itself. A caller passing one would be supplying a command that could disagree with
    the key — which is exactly the drift the derivation exists to prevent.
    """
    with pytest.raises(ValueError, match="builds its own command"):
        console_binding_args(
            "M-s", ConsoleBindingAction.FORWARD_TO_SESSIONS, ("true",), table=ConsoleKeyTable.PREFIX
        )


def test_the_panes_key_runs_our_own_program_from_the_prefix_table() -> None:
    """Folding the column is our program's job, and the key that runs it is free.

    **`-T prefix`, and refused anywhere else** (the test below). The root budget is one key
    (DEC-041) and it is already spent on the way back from a displayed agent; a fold is a
    convenience, and a convenience does not take a key from every agent on this server.

    **It runs our program rather than tmux's own `resize-pane -Z`** for the reason the option
    exists at all: the fold is eight measured resizes plus a zoom plus a window option that
    every later exchange re-applies, and tmux can do none of that from a key. `prefix z`
    remains the instant, animation-free version and is left alone.
    """
    argv = console_binding_args(
        "h",
        ConsoleBindingAction.TOGGLE_PANES,
        ("python", "-m", "remote_agents", "console", "panes"),
        table=ConsoleKeyTable.PREFIX,
    )

    assert argv[:4] == ("bind-key", "-T", "prefix", "h"), (
        f"the fold key must go in the prefix table, not the root one: {argv}"
    )
    assert argv[4] == "run-shell"
    assert "-n" not in argv, "a fold key in the root table would cost every agent a key"
    assert argv[5] == "python -m remote_agents console panes"


def test_the_panes_key_is_refused_in_the_root_table() -> None:
    """Refused where it is built, exactly as a forwarding chord is.

    The argv is otherwise identical, so a caller that asked for the root table would spend the
    console's whole budget a second time and no test of the fold itself would notice.
    """
    with pytest.raises(ValueError, match="prefix table"):
        console_binding_args(
            "h",
            ConsoleBindingAction.TOGGLE_PANES,
            ("true",),
            table=ConsoleKeyTable.ROOT,
        )


def test_the_panes_key_needs_the_command_that_folds_the_column() -> None:
    """A key bound to nothing is worse than an unbound key: it answers, and does nothing.

    The same refusal `SHOW_PROJECTS` carries, for the same measured reason — the projects
    command defaulted to empty for one commit of this branch and every console built without
    one failed to come up at all.
    """
    with pytest.raises(ValueError, match="needs the command"):
        console_binding_args("h", ConsoleBindingAction.TOGGLE_PANES, table=ConsoleKeyTable.PREFIX)
