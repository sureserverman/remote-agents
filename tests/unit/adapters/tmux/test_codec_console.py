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

import re

import pytest

from remote_agents.adapters.tmux.codec import (
    _PROFILE_OPTION,
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


def test_the_root_table_still_installs_a_key_that_runs_our_own_program() -> None:
    """The one assertion worth keeping from the retired prefix layer's tests.

    Those three covered `FORWARD_TO_SESSIONS`, which retired with the Alt chords it carried:
    it bound one prefix key per chord so a chord typed inside a displayed agent could still
    reach the console, and a root function key does not have that problem. Their guard,
    table-refusal and derives-its-own-command properties all have counterparts in the
    `function_key` tests above, asserted against the action that replaced it.

    What was left over is this: `SHOW_PROJECTS` is still a root binding, and it is the one
    action that runs *our program* rather than tmux against tmux. Kept because nothing else in
    this file asserts that the root table installs anything at all.
    """
    argv = console_binding_args("F12", ConsoleBindingAction.SHOW_PROJECTS, ("true",))

    assert argv[:3] == ("bind-key", "-n", "F12")
    assert argv[3] == "run-shell"
    assert "true" in argv[4]


def test_the_panes_key_runs_our_own_program_from_the_prefix_table() -> None:
    """Folding the column is our program's job, and the key that runs it is free.

    **`-T prefix`, and refused anywhere else** (the test below). Every root key is argued for
    one at a time against what it takes from the owner's agents (DEC-093, which supersedes
    DEC-041's budget of one); a fold is a convenience, and a convenience does not earn a key
    every agent on this server can never receive.

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
    script = argv[5]
    assert "exec python -m remote_agents console panes" in script, script

    # **The guard, and it is the price of the key being free.** A key table belongs to the
    # *server* and every managed agent is attached to that same socket, so a prefix binding
    # fires from any client on it unless the script asks who pressed it — the reach DEC-073(3)
    # recorded after reproducing it on the forwarding chords. Without this clause, an owner
    # attached to an agent with the session detail's own `remote-agents attach ra-<uuid>` folds
    # the console's column from a terminal that is not the console.
    #
    # The whole clause, not its parts, for the reason the forwarding chord's twin says: an
    # inverted `!=` still contains `= "ra-console"`, and `|| true` in place of `|| exit 0` is
    # asserted by nothing at all.
    guard = (
        f'test "$(tmux display-message -p "##{{client_session}}")" = "{CONSOLE_SESSION_NAME}" '
        f"|| exit 0;"
    )
    assert guard in script, (
        f"the fold key does not refuse a client attached to anything but the console: {script}"
    )


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


# --- The F-key root layer (sub-plan 03 Stage 2) ------------------------------------------

#: What the registry answers today, passed in rather than imported so these tests pin the
#: *rule* the script encodes and not the measurement, which `tests/provider_contract` owns.
_RESERVED = {
    "claude": frozenset(),
    "codex": frozenset(),
    "opencode": frozenset({"F2"}),
    "cursor-agent": frozenset(),
}


def test_a_function_key_binding_carries_the_guard_and_its_three_branches() -> None:
    """One root key, three destinations, decided at press time from the active pane's own marks.

    **Root, not prefix, and that is the whole point of the layer.** An F-key must work from
    inside a displayed agent, where the agent owns the pane's keyboard — so it cannot be behind
    a prefix the owner would have to press first. The price is DEC-041's currency: a root
    binding is a key every pane on this socket can never receive, which is exactly why the
    third branch exists.

    The three branches, in the order the script decides them:

    * **the active pane is one of ours** — the owner is in a console pane, so the key belongs to
      the surface running there and is sent straight back to it;
    * **the active pane's provider reserves this key** — the agent binds it already, so the
      console hands it over rather than stealing it. That is what stops F2 at an OpenCode pane
      opening Settings instead of cycling the model;
    * **otherwise** — an agent that does not want this key, so it goes to the sessions pane,
      which is the position the layer is *for*.

    Marks are read at press time rather than captured at install (DEC-038): a pane can be
    rebuilt or exchanged while the binding stands, and the mark travels with it.
    """
    argv = console_binding_args(
        "F2", ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=_RESERVED
    )

    assert argv[:3] == ("bind-key", "-n", "F2"), (
        f"an F-key must be a root binding or it cannot reach a displayed agent: {argv}"
    )
    assert argv[3] == "run-shell"
    script = argv[4]

    # The guard, whole rather than in parts — the same reasoning the chord layer's own test
    # records: `!= "ra-console"` still contains `= "ra-console"`, so a substring assertion
    # survives an inverted guard.
    guard = (
        f'test "$(tmux display-message -p "##{{client_session}}")" = "{CONSOLE_SESSION_NAME}" '
        f"|| exit 0;"
    )
    assert guard in script, (
        "an F-key root binding does not refuse a client attached to anything but the console, "
        f"so it fires from any client on this server (DEC-073(3)): {script}"
    )

    assert f"##{{{CONSOLE_SLOT_OPTION}}}" in script, "the script never reads the slot mark"
    assert _PROFILE_OPTION in script, "the script never reads the profile mark"
    assert 'send-keys -t "$active" F2' in script, "no branch sends the key to the active pane"
    assert 'send-keys -t "$panes" F2' in script, "no branch sends the key to the sessions pane"

    # Exactly the provider that reserves F2, and no other name from the table.
    assert '"$profile" = "opencode"' in script, "the reservation never reaches the script"
    for absent in ("claude", "codex", "cursor-agent"):
        assert absent not in script, (
            f"{absent!r} reserves no key and must not appear in the F2 script: {script}"
        )


def test_a_function_key_no_provider_reserves_has_no_pass_through_branch() -> None:
    """The reservation is per key, so a key nobody binds carries no provider name at all.

    Asserted because the opposite is the silent failure: a script that tested every profile
    against an empty set would still work, and would then hand the key to whichever agent
    happened to be there the day someone widened a set by accident.
    """
    script = console_binding_args(
        "F5", ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=_RESERVED
    )[4]

    for absent in _RESERVED:
        assert absent not in script, f"{absent!r} reserves no F5 and is named anyway: {script}"
    assert '"$profile"' not in script, "F5 tests a profile it has no reason to read"
    assert 'send-keys -t "$active" F5' in script, "the console's own panes still get the key"


def test_the_sessions_branch_delivers_to_exactly_one_pane_or_to_nothing() -> None:
    """BL-042's stricter arm, decided here for this path: ambiguity delivers nothing.

    The chord layer took `head -n 1`, which picks a pane when two carry the sessions mark —
    and the key it delivers can be an unconfirmed stop (DEC-018) against a row in whichever
    console the arbitrary winner belongs to. A root key reaches further than a prefix one, so
    this path refuses instead: the count must be exactly one.

    **`grep -c .` rather than `wc -l`, and the difference is the empty case plus a platform.**
    `printf "%s\\n"` of an empty result still emits one line, so `wc -l` would report `1` for
    "no sessions pane at all" and the key would be sent to the empty string. `grep -c .` counts
    only non-empty lines, so nothing matched reads as `0` without a second clause. It also
    sidesteps BSD `wc`, which pads its output — `test "       1" = 1` is false, and this
    project runs on macOS as well as Linux.
    """
    script = console_binding_args(
        "F8", ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=_RESERVED
    )[4]

    assert 'grep -c .)" = 1' in script, (
        f"the sessions branch does not require exactly one marked pane: {script}"
    )
    assert "head -n 1" not in script, (
        "this path must not pick a winner when two panes carry the sessions mark"
    )


def test_f11_is_refused_at_build() -> None:
    """The one F-key the layer leaves alone, refused where it is built rather than omitted.

    F11 is the terminal's own full-screen toggle almost everywhere. Taking it as a root key
    would be taking it from the emulator, and the owner would have no way to tell which side
    swallowed it. Omitting it from the table is what makes it unbound; refusing it here is what
    stops the next author binding it without meeting the argument.
    """
    with pytest.raises(ValueError, match="F11"):
        console_binding_args(
            "F11", ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=_RESERVED
        )


@pytest.mark.parametrize(
    "key",
    ["F2; rm -rf /", "F2\n", "M-F2", "f2", "F0", "F13", "F", "", "C-F2", "F2 ", "F2\n\n"],
)
def test_a_function_key_that_is_not_one_is_refused_before_it_is_interpolated(key: str) -> None:
    """The key is interpolated into a shell string, so the validation is the safety property.

    `console_binding_args`' general check accepts anything alphanumeric behind one optional
    modifier, which is true of `f2`, `M-F2` and `C-F2` — all three would build a script for a
    key tmux either resolves differently or not at all, and the lowercase one is the quiet
    disaster: Textual spells these keys `f2` and tmux spells them `F2`, the two meet in this
    codebase, and tmux would bind a key nothing sends.

    So this branch validates the key *again*, stricter, against the original string rather than
    the modifier-stripped body — and it runs before the script is built, which is the ordering
    the whole injection argument rests on.
    """
    with pytest.raises(ValueError):
        console_binding_args(
            key, ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=_RESERVED
        )


def test_a_function_key_binding_is_refused_outside_the_root_table() -> None:
    """The mirror of the forwarding chord's refusal, and for the opposite reason.

    A chord is affordable *because* it is a prefix key. An F-key is only useful because it is
    a root one: behind a prefix it could never reach a displayed agent, which is the single
    position the layer exists to serve. Refused where it is built, so a caller cannot quietly
    install a key that looks bound and answers nowhere.
    """
    with pytest.raises(ValueError, match="root table"):
        console_binding_args(
            "F2",
            ConsoleBindingAction.FORWARD_FUNCTION_KEY,
            reserved_keys=_RESERVED,
            table=ConsoleKeyTable.PREFIX,
        )


@pytest.mark.parametrize(
    "name",
    [
        'opencode"; rm -rf /; #',
        "opencode\n",
        "opencode\nrm -rf /",
        "open code",
        "opencode$(id)",
        "",
    ],
    ids=[
        "metacharacters",
        "trailing-newline",
        "embedded-newline",
        "space",
        "substitution",
        "empty",
    ],
)
def test_a_function_key_binding_refuses_a_profile_name_it_cannot_safely_interpolate(
    name: str,
) -> None:
    """The reservation's *keys* reach the shell too, and nothing upstream promised they were safe.

    The names come from the curated registry today, so this cannot fire from production input.
    It is here because the argument that makes the key safe — validated before interpolation —
    has to hold for every value that reaches the script, and a reader checking that argument
    should find it covering both rather than having to notice the second one is unguarded.

    **`trailing-newline` is the case this task's review found, and it is why the others are here
    too.** The pattern was anchored `^...$`, and Python's `$` also matches immediately before one
    trailing newline — so `"opencode\n"` passed a check whose whole job was to make
    interpolation safe. It was not an injection, because the value lands inside double quotes
    where POSIX keeps a newline literal; it was a comparison that would silently never match,
    which is the quieter half of the same defect. The first version of this test carried only
    the metacharacter payload, which the pattern already rejected — so it could not have caught
    this. The population is now every shape that must be refused, not the one that reads as
    dangerous.
    """
    with pytest.raises(ValueError, match="profile"):
        console_binding_args(
            "F2",
            ConsoleBindingAction.FORWARD_FUNCTION_KEY,
            reserved_keys={name: frozenset({"F2"})},
        )


def test_a_function_key_binding_refuses_to_be_built_without_the_reservations() -> None:
    """The refusal lives here too, at the layer that actually interpolates the key.

    `ConsoleComposer` refuses the same omission, and that guards the one production caller.
    This guards the *act*: `None` defaulting to an empty mapping is a valid "nobody reserves
    anything", so a future caller reaching `console_binding_args` directly — a maintenance
    command, a second composition root, a debug script — would build a script that silently
    takes OpenCode's F2 from the agent that binds it. Raised by the Stage 2 review, which
    pointed out that a safety property holding only while every caller routes through one
    constructor is not a property of this function.

    Stating `{}` is still allowed and still means what it says.
    """
    with pytest.raises(ValueError, match="reserved_keys"):
        console_binding_args("F2", ConsoleBindingAction.FORWARD_FUNCTION_KEY)

    argv = console_binding_args("F2", ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys={})
    assert argv[:3] == ("bind-key", "-n", "F2")
    assert '"$profile"' not in argv[4], "an empty reservation still tested a profile"


def test_a_function_key_binding_builds_its_own_command() -> None:
    """Like the forwarding chord: the command is derived from the key, never supplied."""
    with pytest.raises(ValueError, match="builds its own command"):
        console_binding_args(
            "F2",
            ConsoleBindingAction.FORWARD_FUNCTION_KEY,
            ("true",),
            reserved_keys=_RESERVED,
        )


def test_the_function_key_script_is_byte_stable_across_builds() -> None:
    """Two builds of one key are the same string, so a rebuilt console reinstalls the same thing.

    Not a style check. The reservation arrives as a mapping, and a script built by iterating it
    would reorder whenever the registry's insertion order changed — which makes every console
    rebuild a diff, and makes a byte comparison useless as a way of asking whether the installed
    bindings are the ones this version emits.

    **Driven with two providers reserving one key, which today's registry does not have.** The
    first version of this test used the real table, where only OpenCode reserves F2 — so the
    script had one name in it, every ordering produced the same string, and dropping the `sorted`
    left the test green. A test that cannot fail is worse than no test, so the population here is
    the one where order is observable rather than the one that happens to ship.
    """
    two = {
        "opencode": frozenset({"F2"}),
        "another-agent": frozenset({"F2"}),
        "codex": frozenset(),
    }
    shuffled = dict(reversed(list(two.items())))

    first = console_binding_args("F2", ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=two)
    second = console_binding_args(
        "F2", ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=shuffled
    )

    assert first == second, "the script depends on the order the reservation happens to arrive in"
    assert first[4].count('"$profile" = ') == 2, (
        f"the fixture must put two names in the script or order cannot be observed: {first[4]}"
    )


def test_no_console_binding_script_carries_a_raw_control_character() -> None:
    """Every script this module builds is one shell string, so a raw newline in it is a defect.

    **Found the hard way, in this task.** The sessions branch counts panes with
    `printf "%s\\n"`, and the format has to reach `/bin/sh` as the two characters `\\` and `n`.
    Written as a single escape in the Python source it becomes an *actual* newline instead, at
    which point `printf` prints a newline rather than the pane list, `grep -c .` counts zero,
    and the branch silently delivers nothing — a key that does nothing, on every press, with no
    error anywhere. The build still succeeded and every other assertion in this file still
    passed.

    So the property is asserted over **every** action rather than fixed in the one place it bit:
    a control character in a `run-shell` string is never intended here, and the next script to
    grow a format string gets the same protection without anyone remembering to ask for it.
    """
    built = {
        "F2": console_binding_args(
            "F2", ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=_RESERVED
        ),
        "F12": console_binding_args("F12", ConsoleBindingAction.SHOW_PROJECTS, ("true",)),
        "z": console_binding_args(
            "z", ConsoleBindingAction.TOGGLE_PANES, ("true",), table=ConsoleKeyTable.PREFIX
        ),
    }

    offenders = {
        key: repr(argv[-1])
        for key, argv in built.items()
        if any(character in argv[-1] for character in "\n\r\t\x00")
    }
    assert not offenders, (
        f"these binding scripts carry a raw control character, which never survives as the "
        f"format or argument it was meant to be: {offenders}"
    )


# --- console facelift sub-plan 2 Task 1.5: what the panes publish to the bar -------------------


def test_the_selected_flag_is_a_console_window_option_and_none_unsets_it() -> None:
    from remote_agents.adapters.tmux.codec import session_selected_args
    from remote_agents.ports.console import SESSION_SELECTED_OPTION

    target = console_target()
    assert session_selected_args(True) == (
        "set-option",
        "-w",
        "-t",
        target,
        SESSION_SELECTED_OPTION,
        "1",
    )
    assert session_selected_args(False) == (
        "set-option",
        "-w",
        "-t",
        target,
        SESSION_SELECTED_OPTION,
        "0",
    )
    assert session_selected_args(None) == (
        "set-option",
        "-w",
        "-u",
        "-t",
        target,
        SESSION_SELECTED_OPTION,
    )


def test_the_typing_flag_is_a_console_window_option_and_none_unsets_it() -> None:
    from remote_agents.adapters.tmux.codec import typing_args
    from remote_agents.ports.console import TYPING_OPTION

    target = console_target()
    assert typing_args(True) == ("set-option", "-w", "-t", target, TYPING_OPTION, "1")
    assert typing_args(False) == ("set-option", "-w", "-t", target, TYPING_OPTION, "0")
    assert typing_args(None) == ("set-option", "-w", "-u", "-t", target, TYPING_OPTION)


def _marks(*readings):
    from remote_agents.ports.console import RemoteControlMark

    return tuple(RemoteControlMark(provider, word, tone) for provider, word, tone in readings)


def test_remote_control_words_publish_the_full_and_compact_values() -> None:
    from bar_console import NIGHT

    from remote_agents.adapters.tmux.codec import remote_control_words_args
    from remote_agents.ports.console import (
        REMOTE_CONTROL_COMPACT_OPTION,
        REMOTE_CONTROL_OPTION,
        RemoteControlTone,
    )

    full, compact = remote_control_words_args(
        _marks(
            ("claude", "on", RemoteControlTone.ON),
            ("codex", "off", RemoteControlTone.OFF),
        ),
        NIGHT,
    )

    assert full[:5] == ("set-option", "-w", "-t", console_target(), REMOTE_CONTROL_OPTION)
    assert full[5] == (
        f"Remote Control  claude #[fg={NIGHT.on}]on#[fg={NIGHT.muted}]"
        f" · codex #[fg={NIGHT.off}]off#[fg={NIGHT.muted}]"
    )
    assert compact[:5] == (
        "set-option",
        "-w",
        "-t",
        console_target(),
        REMOTE_CONTROL_COMPACT_OPTION,
    )
    assert compact[5] == f"RC #[fg={NIGHT.on}]●#[fg={NIGHT.off}]○#[fg={NIGHT.muted}]"


def test_remote_control_words_mark_each_tone_with_its_own_glyph() -> None:
    """R6 / DEC-010: the glyph carries the state, the colour repeats it."""
    from bar_console import NIGHT

    from remote_agents.adapters.tmux.codec import remote_control_words_args
    from remote_agents.ports.console import RemoteControlTone

    glyphs = {}
    for tone in RemoteControlTone:
        _, compact = remote_control_words_args(_marks(("codex", "x", tone)), NIGHT)
        glyphs[tone] = re.sub(r"#\[[^]]*\]", "", compact[5]).removeprefix("RC ")

    assert glyphs == {
        RemoteControlTone.ON: "●",
        RemoteControlTone.OFF: "○",
        RemoteControlTone.BROKEN: "!",
        RemoteControlTone.UNKNOWN: "?",
    }


def test_remote_control_words_none_unsets_both_values() -> None:
    from bar_console import NIGHT

    from remote_agents.adapters.tmux.codec import remote_control_words_args
    from remote_agents.ports.console import REMOTE_CONTROL_COMPACT_OPTION, REMOTE_CONTROL_OPTION

    assert remote_control_words_args(None, NIGHT) == (
        ("set-option", "-w", "-u", "-t", console_target(), REMOTE_CONTROL_OPTION),
        ("set-option", "-w", "-u", "-t", console_target(), REMOTE_CONTROL_COMPACT_OPTION),
    )


def test_remote_control_words_with_tmux_metacharacters_draw_literally() -> None:
    """A word carrying `#`, `,` or `}` reaches the owner's screen as typed, never as a format.

    Drawn by a real client: `display-message -p` does not run the draw pass where `##` and
    `#[` are interpreted, so only the status row itself can show the escape is right.
    """
    from bar_console import NIGHT, BarConsole

    from remote_agents.adapters.tmux.codec import remote_control_words_args
    from remote_agents.ports.console import RemoteControlTone

    word = "a#[fg=red]b,c}d#{e}"
    bar = BarConsole(200)
    try:
        bar.start()
        full, _ = remote_control_words_args(
            _marks(("co#dex", word, RemoteControlTone.UNKNOWN)), NIGHT
        )
        bar.run(full)
        row = bar.settled_row(lambda row: "Remote Control" in row)
    finally:
        bar.close()

    assert f"Remote Control  co#dex {word}  {CONSOLE_SESSION_NAME}" in row, repr(row)
