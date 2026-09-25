"""Pinned tmux 3.4 pane format and strict managed-session decoding."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.console import (
    REMOTE_CONTROL_COMPACT_OPTION,
    REMOTE_CONTROL_OPTION,
    SESSION_SELECTED_OPTION,
    TYPING_OPTION,
    ConsoleBindingAction,
    ConsoleKeyTable,
    ConsolePaneSlot,
    RemoteControlMark,
    RemoteControlTone,
    StatusBarKey,
    StatusBarPalette,
)

_DELIMITER = "|"

# Schema 1 marked the *session*; schema 2 marks the *pane*. Both are decodable, because an
# owner's already-running sessions must survive the upgrade, and a schema-1 session has no
# pane mark to find. The version is what tells the two shapes apart on a single line, and it
# has to, because tmux format expansion falls back pane -> session: a schema-1 session's
# pane reports the session's mark as if it were its own (verified, tmux 3.4, 2026-08-19).
_SCHEMA_VERSION = "1"
_PANE_SCHEMA_VERSION = "2"
_DECODABLE_SCHEMA_VERSIONS = frozenset({_SCHEMA_VERSION, _PANE_SCHEMA_VERSION})

# The console session carries the `ra-` prefix so it visibly belongs to this socket, and a
# non-UUID suffix so `exact_session_target` can never accept it: no lifecycle code path can
# address the console as a managed session by construction rather than by discipline.
CONSOLE_SESSION_NAME = "ra-console"
# The console's own surface pane, marked so recovery can find it wherever an exchange parked
# it. Deliberately *not* one of the four identity options above and not part of that
# vocabulary: it says "this pane belongs to the console", never "this pane is a session".
# Pane-scoped, so it travels with the surface exactly as identity travels with an agent — and
# so nothing inherits it, since neither the console session nor a managed one sets it.
CONSOLE_SLOT_OPTION = "@remote_agents_console_slot"
SURFACE_SLOT = "surface"
# Which session the console's sessions pane has highlighted, readable by every pane process on
# this console. **Session-scoped, and that is the deliberate part.**
#
# DEC-038 puts identity on the *pane*, because a session-scoped mark stays behind while the
# pane travels and then describes whatever swapped in. That argument is about identity and it
# does not reach this: a selection is one fact about the console as a whole, written by the one
# pane that owns a cursor and read by the three that do not. Pane scope here would give each
# reader a private copy of a shared fact — which is DEC-038's own failure mode, running in the
# other direction. It is not identity, it says nothing about which pane is which, and no reader
# may treat it as either.
#
# DEC-038's *other* mechanism does still apply and is named here so a reader does not have to
# rediscover it: a session-scoped option is reported by every pane in that session through
# tmux's pane -> session fallback. Under DEC-040 the console window hosts a displaced agent's
# pane, so that pane answers this option too. Harmless, because neither `PANE_FORMAT` nor
# `ARRANGEMENT_FORMAT` expands it and no reader asks a pane for it — but it is the reason this
# option must never be read *per pane* to mean anything about that pane.
#
# Measured on tmux 3.4: user options do **not** inherit session <- global, so a `set -g` of
# this name left over from someone debugging is invisible to a session-scoped
# `show-options -qv`. The sibling `CONSOLE_SLOT_OPTION` asserts its own non-inheritance and this
# one now does too, because "where could a value we did not write come from" is the question a
# reader of a key's input actually has. Written without respelling the option: the vocabulary
# test counts occurrences, and it caught this comment doing so -- which is the check working.
#
# It dies with `ra-console`, so a stale selection cannot outlive the console that published it
# -- though it *can* outlive the sessions **pane** that published it, which is a different and
# narrower residual, recorded on `SessionsPaneScreen._publish_selection`.
SELECTED_SESSION_OPTION = "@remote_agents_selected_session"

# The four identity option names, spelled **once each** and referenced everywhere else in this
# module. They are written by `pane_mark_args` and read back by two format strings, and those
# three uses only agree because they are generated from one place — so the name itself is a
# constant rather than a literal repeated per use. Pinned by
# `tests/architecture/test_the_mark_vocabulary_has_one_home.py`, which counts occurrences: it
# caught `ARRANGEMENT_FORMAT` re-spelling two of them, which is the drift it exists for.
_SCHEMA_OPTION = "@remote_agents_schema"
_ID_OPTION = "@remote_agents_id"
_PROJECT_OPTION = "@remote_agents_project_id"
_PROFILE_OPTION = "@remote_agents_profile"

#: Profile ids that no longer exist, and the curated id each one is now read as.
#:
#: **A read, never a write.** `pane_mark_args` stamps only ids `closed_profiles()` curates, so
#: nothing new ever lands here; this table exists for panes that were already running when the
#: id was retired, and whose mark is stamped pane-scoped where nothing can rewrite it.
#:
#: `claude-remote` was `claude --remote-control {managed_name}` -- the same binary under a
#: second id -- and Stage 4 made that flag a property of the launch instead. Migration 13
#: rewrites the stored rows; this is the other half, because a pane outlives the deploy.
#: Without it a legacy pane reports a profile its own migrated record no longer names, and
#: `session_actions.pane_is_attachable` refuses it: the session stays listed as running and
#: becomes unreachable, since `copy_attach` is the only route to it.
#:
#: One of exactly two places the retired id survives as a value; the other is migration 13.
#: Both are reads of history, which is why neither is a table any surface offers.
_RETIRED_PROFILE_IDS = {"claude-remote": "claude"}
# Who is where, in one listing: the read the swap composer derives its whole answer from.
# Deliberately separate from PANE_FORMAT, which is lifecycle evidence and drops the console's
# own view — the arrangement needs exactly what that drops (a console pane is half of every
# exchange) and needs position, which lifecycle never cares about.
ARRANGEMENT_FORMAT = _DELIMITER.join(
    (
        "#{session_name}",
        "#{window_index}",
        "#{pane_index}",
        "#{pane_id}",
        f"#{{{_SCHEMA_OPTION}}}",
        f"#{{{_ID_OPTION}}}",
        f"#{{{CONSOLE_SLOT_OPTION}}}",
    )
)
PANE_FORMAT = _DELIMITER.join(
    (
        "#{session_name}",
        "#{session_id}",
        "#{pane_id}",
        "#{pane_pid}",
        "#{pane_dead}",
        "#{pane_dead_status}",
        f"#{{{_SCHEMA_OPTION}}}",
        f"#{{{_ID_OPTION}}}",
        f"#{{{_PROJECT_OPTION}}}",
        f"#{{{_PROFILE_OPTION}}}",
    )
)


@dataclass(frozen=True, slots=True)
class ManagedPane:
    """Trusted tmux metadata decoded from the pinned format-version contract."""

    session_name: str
    """The session this *line* lists the pane under — not, by itself, the session hosting it.

    tmux reports a linked window's pane once per session linked to it, so one pane yields
    several lines with different names here. `inventory` resolves that by keeping the line
    under the pane's own session whenever there is one, which makes the surviving value mean
    "the session showing this pane": its own, or another when nothing lists it at home.

    Never a lifecycle target. Build one from `session_id` — a schema-2 pane keeps its
    identity wherever tmux lists it, and this field is exactly the part that moves."""

    pane_id: str

    pane_scoped: bool
    """Whether the identity was read from the pane's *own* mark (schema 2) rather than
    inherited from its session (schema 1). Only a pane-scoped mark makes `pane_id` an
    address: an inherited one says which session the pane sits in, which is what the pane
    id would have told you anyway, and stops being true the moment anything moves."""

    session_id: SessionId
    project_id: ProjectId
    profile_id: ProfileId
    process_id: int
    live: bool
    preserved: bool


def pane_owned_identity(schema: str, raw_id: str) -> SessionId | None:
    """Decode the identity a pane carries **in its own right**, or None if it carries none.

    The one rule, called by both readers of the server. `parse_pane` decodes lifecycle
    evidence and `parse_arrangement` decodes where panes are; each needs to know whether a
    mark belongs to the pane or was inherited from its session, and each had its own copy of
    the answer. Two copies of a rule that turns on a schema version is one schema bump away
    from disagreeing — and disagreeing means one reader treats a displaced surface as the
    agent while the other does not.

    Only schema 2 is the pane's own. tmux resolves `#{@option}` by falling back pane ->
    session, so under schema 1 every pane in a session's window reports that session's id
    whether or not it is the agent (DEC-038).
    """
    if schema != _PANE_SCHEMA_VERSION or not raw_id:
        return None
    return SessionId.parse(raw_id)


def exact_session_target(session_name: str) -> str:
    """Return tmux's exact session target for one strict opaque managed name."""
    if not session_name.startswith("ra-"):
        raise ValueError("managed session name must start with ra-")
    try:
        session_id = SessionId.parse(session_name.removeprefix("ra-"))
    except ValueError as error:
        raise ValueError("managed session name must contain a canonical UUID") from error
    return f"ra-{session_id}:"


def exact_pane_target(pane_id: str) -> str:
    """Return tmux's exact pane target for one decoded pane id, refusing anything else.

    The closed shape `exact_session_target` has, for the address that replaces it on every
    operation that must follow the agent rather than the window it started in. A pane id is
    `%` followed by digits and nothing more — no whitespace, no session form, no free text —
    so an id that came from anywhere but our own inventory cannot reach an argv (DEC-001).
    """
    if not pane_id.startswith("%"):
        raise ValueError("a pane target must be a tmux pane id")
    digits = pane_id.removeprefix("%")
    if not digits or not digits.isascii() or not digits.isdigit():
        raise ValueError("a pane target must be % followed by digits")
    return pane_id


def swap_pane_args(source_pane: str, target_pane: str) -> tuple[str, ...]:
    """Return the argv suffix that exchanges two decoded panes, leaving focus alone.

    **Both ends go through `exact_pane_target`.** Every other single-target operation has one
    address to get right; an exchange has two, and a session target on *either* end is a
    window target tmux resolves to whichever pane sits there now — so the wrong end puts an
    agent into a stranger's window and crosses two identities (DEC-038). There is no
    "obviously the console" end to relax: the console's left slot is a position whose
    occupant changes with every exchange, which is exactly what a pane id pins and a window
    target does not.

    **`-d` is the mechanism refusing to make a presentation decision.** Without it tmux makes
    the target position active, so the client jumps to the left slot on every exchange —
    right when the owner opened a session, wrong when a background recovery unwound a
    half-swapped console under them. Focus belongs to whoever asked for the swap, so the
    exchange never moves it and the surface selects when it means to. Verified on tmux 3.4
    (2026-08-19) rather than read off the manual: with the console's *right* pane active, a
    bare `swap-pane` left the swapped-in pane active at index 0, and `-d` left the right pane
    active. Pinned as Claim 12.

    Session-destroying by construction it is not: `swap-pane` exchanges two panes and leaves
    both windows non-empty. That is the whole reason this design exchanges rather than *moves*
    a pane — moving a single-pane session's only pane empties its window, and tmux destroys the
    window and the session with it (probed 2026-08-19). DEC-036 records that rejected shape by
    name, with its evidence; it is deliberately not named here, because a gate check greps this
    tree for the command precisely so it can never be built, and the register is where the
    argument for not building it belongs.
    """
    return (
        "swap-pane",
        "-d",
        "-s",
        exact_pane_target(source_pane),
        "-t",
        exact_pane_target(target_pane),
    )


def pane_mark_args(
    session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
) -> tuple[tuple[str, ...], ...]:
    """Return the argv suffixes that stamp schema-2 identity onto one launched pane.

    **`-p` on every field, and no session-scoped twin.** The mark has to travel with the
    pane, which `set-option -p` does — but the absence of the session-scoped copy is the
    load-bearing half, because tmux resolves `#{@option}` by falling back pane -> session.
    Marked on both scopes, a home session keeps answering with its identity after its agent
    has moved out, so whatever pane swaps in inherits it and two panes report one session
    (verified on tmux 3.4, 2026-08-19). Marked on the pane alone, the identity goes exactly
    where the agent goes and the arriving pane carries nothing.

    The target is the session at launch time, when its only pane is the one being marked;
    thereafter the pane is addressed by the id `exact_pane_target` validates.

    **The schema mark is written last, and the order is load-bearing.** These four are
    separate `set-option` calls with no transaction around them, and `pane_owned_identity`
    reads the schema alone to decide whether a pane owns its identity — so the schema *is*
    the commit record for the other three. Written first, it would be true the instant it
    landed: `raw_id` already reads non-empty on a schema-1 pane by tmux's pane -> session
    fallback, so a run interrupted after the schema write would leave a pane reporting
    `pane_scoped` with its project and profile still session-scoped, and
    `upgrade_pane_identity` skips exactly those. The repair would then decline to repair it,
    permanently, while the project mark resolved from whatever session the pane was later
    displaced into — the crossing DEC-038 exists to prevent. (Named in prose rather than
    spelled, because `test_the_mark_vocabulary_has_one_home` pins how many times each option
    name appears here, and a mention is indistinguishable from a second vocabulary.)

    Written last, a partial failure leaves the schema at 1, the pane is retried on the next
    run, and the re-issued `set-option` calls are idempotent. Both callers get this: the
    launch path and `upgrade_pane_identity`.
    """
    target = exact_session_target(f"ra-{session_id}")
    return tuple(
        ("set-option", "-p", "-t", target, option, value)
        for option, value in (
            (_ID_OPTION, str(session_id)),
            (_PROJECT_OPTION, str(project_id)),
            (_PROFILE_OPTION, str(profile_id)),
            (_SCHEMA_OPTION, _PANE_SCHEMA_VERSION),
        )
    )


def attach_host_target(session_id: SessionId, host: str | None) -> str:
    """Return the exact target for attaching to the session *showing* one agent's pane.

    A tmux client attaches to a session, so attach is the one agent-reaching operation that
    cannot be answered with a pane id the way capture, send-keys and destruction are
    (DEC-038). It is answered with the session the pane is currently hosted by, which under
    the swap model is the console while that agent is displayed and its own session
    otherwise. That is the re-scoping DEC-021's read-only attach needed before any pane
    displacement could ship: without it a copyable command silently lands the owner in a
    terminal showing the projects surface, with nothing reporting an error.

    **Closed, like every other target builder.** `host` is text decoded from our own
    inventory, and the value of a closed shape is precisely that it does not depend on that
    provenance holding: the console's own name exactly, or a canonical `ra-<uuid>` that
    `exact_session_target` validates, and nothing else reaches an argv (DEC-001).

    A host equal to the session's own name is not special-cased — it takes the same route and
    produces the identical target, so the ordinary case cannot drift from the displaced one.
    A host that is a *different* managed session is honored rather than refused: a crossed
    pane is the state recovery exists to unwind, and it has to stay reachable while it lasts.
    """
    if host is None:
        return exact_session_target(f"ra-{session_id}")
    if host == CONSOLE_SESSION_NAME:
        return console_target()
    return exact_session_target(host)


def attach_argv(
    session_id: SessionId, *, read_only: bool = False, host: str | None = None
) -> tuple[str, ...]:
    """Return the exact argument vector that attaches to one managed session.

    `host` names the session currently showing the agent's pane; omitted, the agent is
    assumed to be at home. See `attach_host_target` for why attach names a session at all.

    `read_only` adds tmux's own `-r` and nothing else. It is what a PRESERVED session is
    offered (DEC-021): the pane's output is the thing PRESERVED exists to keep, and refusing
    to show it made the state less useful than what it replaced — but the agent has exited, so
    there is nothing to type *to*, and a writable attach would imply otherwise.

    A flag on the one builder rather than a second function, so the socket and the exact
    target cannot drift between the two forms. That target is still `exact_session_target`,
    which refuses anything that is not a canonical managed name — read-only widens *what may
    be attached to*, never *what may be named*.

    **`-r` goes after `attach-session`, not before it.** It is a flag of the command, not a
    global tmux option: `tmux -L remote-agents -r attach-session …` exits with
    `unknown option -- r`, because the global set is `[-2CDlNuVv] [-c] [-f] [-L] [-S] [-T]`
    and `-r` is not in it. Verified against tmux 3.4 rather than assumed — the first draft of
    this function put it in the global position and the first draft of its test asserted that
    position, so the pair agreed with each other and not with tmux.
    """
    return (
        "tmux",
        "-L",
        "remote-agents",
        "attach-session",
        *(("-r",) if read_only else ()),
        "-t",
        attach_host_target(session_id, host),
    )


def attach_command(
    session_id: SessionId, *, read_only: bool = False, host: str | None = None
) -> str:
    """Return the one copyable attach command for a currently verified managed session."""
    return " ".join(attach_argv(session_id, read_only=read_only, host=host))


def console_target() -> str:
    """Return tmux's exact session target for the one console session."""
    return f"{CONSOLE_SESSION_NAME}:"


def console_attach_argv() -> tuple[str, ...]:
    """Return the full production argv that attaches a bare shell to the console.

    The full form for the same reason `attach_argv` and `switch_client_argv` carry one:
    the composition root execs this without assembling a tmux invocation of its own.
    """
    return ("tmux", "-L", "remote-agents", "attach-session", "-t", console_target())


#: Which characters a bindable key may be made of, once one optional modifier is stripped.
_BINDABLE_KEY_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


#: How a forwarding binding finds the sessions pane, in tmux's own language.
#:
#: **Resolved at press time, by the slot mark, by tmux itself.** A pane id captured when the
#: binding was installed would forward the key into whatever holds that number after the pane is
#: rebuilt; the mark travels with the pane and survives it (DEC-038), so no *pane id* of ours
#: has to still be right when the key is pressed.
#:
#: One name does: the guard compares the pressing client's session against
#: `CONSOLE_SESSION_NAME`, so renaming `ra-console` makes every key on this route inert. That
#: is the deliberate trade for failing closed — see `_forward_function_key_command`.
#:
#: `$TMUX` is inherited by `run-shell`'s child, so the bare `tmux` here reaches the same server
#: without the socket being spelled again. Measured on tmux 3.4 rather than read off the manual,
#: together with the delivery itself: the emitted argv resolves the marked pane and the key
#: arrives in it.
#: The clause every console prefix binding starts with: do nothing unless the client that
#: pressed the key is attached to the console.
#:
#: **Spelled once, because a key table belongs to the server and every managed agent is
#: attached to that same socket.** Without it a prefix binding fires from a plain
#: `remote-agents attach ra-<uuid>` — a terminal with no console pane on screen at all —
#: which is the reach DEC-073(3) recorded after reproducing it. It was written inline in the
#: forwarding script and is lifted here because the fold key needs the identical sentence: a
#: second copy is a second thing to get wrong, and the one that is wrong will be the quiet one.
_PRESSED_FROM_THE_CONSOLE = (
    f'test "$(tmux display-message -p "#{{client_session}}")" = "{CONSOLE_SESSION_NAME}" || exit 0;'
)


def _console_only_command(command: tuple[str, ...]) -> tuple[str, ...]:
    """Run our own argv, but only for a client attached to the console.

    `SHOW_PROJECTS` spends a *root* key and takes this on the chin — DEC-041 costed that key in
    keystrokes taken from agents, and its reach from a foreign client was accepted with it. A
    prefix binding is a different bargain: it is free precisely because tmux takes it in the
    client, which is the same fact that makes it fire from **every** client on the server. So a
    prefix binding of ours asks who pressed it (DEC-073(3)).

    `exec` because the shell has nothing left to do: the guard has already decided, and an
    extra process between tmux and our program buys nothing.
    """
    return ("sh", "-c", f"{_PRESSED_FROM_THE_CONSOLE} exec {shlex.join(command)}")


#: A function key, as tmux spells it. F1-F12 and nothing else.
#:
#: **Stricter than `_BINDABLE_KEY_CHARACTERS`, and matched against the key as given rather than
#: against the modifier-stripped body.** The general check accepts anything alphanumeric behind
#: one optional `C-`/`M-`, which is true of `f2`, `M-F2` and `C-F2`. All three would build a
#: script, and the lowercase one is the quiet failure: Textual spells these keys `f2`, tmux
#: spells them `F2`, the two meet in this codebase, and `bind-key f2` binds a key nothing sends.
#:
#: **`\A`/`\Z`, never `^`/`$`.** Python's `$` also matches immediately before a single trailing
#: newline, so `^...$` would accept `"F2\n"` — and `re.match` anchors only the start. Here the
#: character-set check above happens to reject a newline before this pattern is consulted, so
#: the gap is masked rather than open; it is closed anyway, because the masking is a property
#: of one other check's position and this pattern is written as if it were self-sufficient.
_FUNCTION_KEY = re.compile(r"\AF(?:[1-9]|1[0-2])\Z")

#: What a profile may be called, checked because the reservation's *keys* are interpolated too.
#:
#: They come from the curated registry, so this cannot fire from anything an owner types. It
#: exists because the argument that makes this script safe is "every value was validated before
#: interpolation", and an argument with one unguarded value in it is not that argument.
#:
#: **`\A`/`\Z` for the reason `_FUNCTION_KEY` gives, and here it was not masked.** Nothing else
#: inspects a profile name, so `^...$` really did admit one ending in a single newline — found
#: by this task's own review. The interpolation site is inside double quotes, where POSIX
#: preserves a newline literally rather than treating it as a separator, so the effect would
#: have been a comparison that silently never matches rather than an injection. A validation
#: that is only safe because of where its output happens to land is not the invariant this
#: module claims, so the pattern is what changed rather than the argument.
_PROFILE_NAME = re.compile(r"\A[A-Za-z0-9_-]+\Z")

#: The key the terminal keeps. Refused here as well as omitted from the console's table.
#:
#: F11 is the full-screen toggle in almost every emulator. A root binding would take it from
#: the emulator, and the owner pressing it would have no way to tell which side swallowed it.
#: Leaving it out of `CONSOLE_BINDINGS` is what makes it unbound; refusing it here is what
#: stops the next author binding it without first meeting that argument.
_TERMINALS_OWN_KEY = "F11"


def _forward_function_key_command(
    key: str, reserved_keys: Mapping[str, frozenset[str]]
) -> tuple[str, ...]:
    """The `sh -c` argv for one root F-key: three destinations, decided from the active pane.

    **Every value here is validated before it reaches the string.** `key` against
    `_FUNCTION_KEY` and each profile name against `_PROFILE_NAME`, both in
    `console_binding_args` immediately before this is called. That ordering is the whole of the
    injection argument: weakening or moving either check is a security change, not a refactor.

    The branches, in the order the script decides them:

    1. **the active pane carries a console slot mark** — the owner is in one of our own panes,
       so the surface process there owns the key and it is sent straight back;
    2. **the active pane's profile reserves this key** — the agent binds it already
       (`reserved_keys`, declared by the provider's own descriptor per DEC-070), so the console
       hands it over rather than stealing it;
    3. **otherwise** — the sessions pane, found by its slot mark.

    **Branch 3 refuses ambiguity rather than picking a winner**, which is where this parts
    company with the retired prefix layer's `head -n 1`. That one was a prefix key; this is a
    root key, and the key it delivers can be an unconfirmed stop (DEC-018) — so two panes
    carrying the sessions mark deliver nothing at all. `grep -c .` rather than `wc -l`: an empty
    result still prints one line through `printf`, and BSD `wc` pads its output, so counting
    non-empty lines is both the correct arithmetic and the portable one.

    Marks are read at press time (DEC-038). A pane is rebuilt by an exchange while the binding
    stands, and the mark travels with the pane; a pane id captured at install would not.

    **`display-message -p` carries no `-t`, and that is the load-bearing assumption here.**
    Branch 1 and branch 2 both ask "which pane is the owner in", so if an unscoped
    `#{pane_id}` resolved to anything but the pane the key was pressed in, every function key
    would misroute. Measured on tmux 3.4 rather than read off the manual, with a **real
    attached client** pressing a **root** binding — which is the only mechanism that exercises
    this, since `send-keys` writes into a pane and never consults a key table: on a two-pane
    session the script recorded `%0` with the first pane active and `%1` after selecting the
    second, so the resolution follows the active pane press by press.
    """
    reserving = sorted(name for name, keys in reserved_keys.items() if key in keys)
    # Sorted, because the mapping arrives from a registry fold whose insertion order is not part
    # of anyone's contract, and a script that reordered with it would make every console rebuild
    # a diff — which would in turn make a byte comparison useless for asking whether what is
    # installed is what this version emits.
    mine_or_reserved = 'test -n "$slot"' + "".join(
        f' || test "$profile" = "{name}"' for name in reserving
    )
    reads_profile = (
        f'profile=$(tmux show-options -qv -pt "$active" {_PROFILE_OPTION}); ' if reserving else ""
    )
    script = (
        f"{_PRESSED_FROM_THE_CONSOLE} "
        f'active=$(tmux display-message -p "#{{pane_id}}"); '
        f'slot=$(tmux show-options -qv -pt "$active" {CONSOLE_SLOT_OPTION}); '
        f"{reads_profile}"
        f'if {mine_or_reserved}; then tmux send-keys -t "$active" {key}; exit 0; fi; '
        f'panes=$(tmux list-panes -a -F "#{{pane_id}}" '
        f'-f "#{{==:#{{{CONSOLE_SLOT_OPTION}}},{ConsolePaneSlot.SESSIONS.value}}}"); '
        f'test "$(printf "%s\\n" "$panes" | grep -c .)" = 1 '
        f'&& tmux send-keys -t "$panes" {key}'
    )
    return ("sh", "-c", script)


def console_binding_args(
    key: str,
    action: ConsoleBindingAction,
    command: tuple[str, ...] = (),
    table: ConsoleKeyTable = ConsoleKeyTable.ROOT,
    *,
    reserved_keys: Mapping[str, frozenset[str]] | None = None,
) -> tuple[str, ...]:
    """Return the argv suffix that installs one console binding, root or prefix, on our socket.

    **The two tables cost different things.** `-n` is the root table: no prefix, so the key is
    one every agent on this server can never receive, for as long as it is bound — which is why
    the key is validated here rather than trusted, and why the *set* is declared in one place in
    the application layer rather than accumulated. `-T prefix` costs an agent nothing, because
    tmux takes the prefix in the client; it costs something else instead, which
    `_PRESSED_FROM_THE_CONSOLE` carries: a key table is the *server's*, so a binding in either
    table fires from every client on it unless the script asks who pressed it.

    A `SHOW_PROJECTS` binding with nothing to run is refused rather than installed as a key
    that quietly does nothing — which is not hypothetical: the composer's projects command
    defaulted to empty for one commit of this branch, and every console built without one
    failed to come up at all.

    **Two escapes, for two interpreters, and missing either one is a defect.** `run-shell`
    takes a single shell string rather than an argv, so the command is joined with
    `shlex.join`: an unquoted join makes any path with a space in it a different command, and
    the composition root's own interpreter path is exactly the kind of thing that has spaces
    on some hosts. But `/bin/sh` is not the only reader — **tmux expands the string as a
    FORMAT first**, so `#` is a metacharacter before the shell ever sees it. Probed on real
    tmux 3.4 rather than read off the manual: `run-shell "echo '#{pane_id}'"` printed `%0`,
    and `run-shell "echo '#(id -u)'"` printed nothing at all, because tmux ran the `#(...)`
    through its own format engine and substituted the result. `shlex.quote` does not escape
    `#` — it is not a shell metacharacter in that position — so doubling it here is what
    closes the gap. The same probe confirms the escape: `##{pane_id}` came back as the literal
    `#{pane_id}`.

    **A value *is* interpolated now, and the paragraph this replaces said the opposite.**
    `SHOW_PROJECTS` still takes a fixed tuple built from `sys.executable`, so nothing
    owner-controlled reaches it. `FORWARD_FUNCTION_KEY` builds its own command by interpolating
    `key` — and every reserving profile's name — into a shell string
    (`_forward_function_key_command`), which is the "future binding built from a value" the old
    paragraph warned about.

    It is safe, and it is safe for one reason worth naming precisely: the alphanumeric
    validation immediately below runs **before** the action branch, so by the time the script is
    built `key` has been proved to be `[C-|M-]?[A-Za-z0-9]+` — no quote, no `$`, no backtick, no
    space, no `#` can survive it. **Moving or weakening that check is a shell-injection change,
    not a refactor.**
    """
    body = key
    for modifier in ("C-", "M-"):
        if body.startswith(modifier):
            body = body.removeprefix(modifier)
            break
    if not body or not set(body) <= _BINDABLE_KEY_CHARACTERS:
        raise ValueError(
            "console binding key must be alphanumeric, optionally behind one C- or M- modifier"
        )
    if not isinstance(table, ConsoleKeyTable):
        # The annotation is enforced by nobody — this repo runs ruff and pytest, no type
        # checker — so a string here would otherwise reach `table.value` and raise
        # `AttributeError` deep in the argv build. The test that used to pin a `ValueError`
        # was deleted on the argument that a closed set leaves no third value to pass; this
        # is what makes that argument true at runtime rather than only for a type checker.
        raise ValueError(f"a console binding's table is a ConsoleKeyTable, not {table!r}")
    if action is ConsoleBindingAction.FORWARD_FUNCTION_KEY:
        if table is not ConsoleKeyTable.ROOT:
            # The mirror of the retired prefix forward's refusal, and for the opposite
            # reason: a prefix key was affordable *because* tmux takes the prefix in the
            # client, and an F-key is only useful because it is a root one. Behind a prefix
            # it could never reach a displayed agent, which is the single position this
            # layer exists to serve.
            raise ValueError("a function-key forward may only be bound in the root table")
        if command:
            raise ValueError("the function-key binding builds its own command")
        if key == _TERMINALS_OWN_KEY:
            raise ValueError(f"{_TERMINALS_OWN_KEY} belongs to the terminal and is not bound here")
        if not _FUNCTION_KEY.match(key):
            raise ValueError(
                "a function-key forward binds F1-F12 in tmux's own spelling, with no modifier"
            )
        if reserved_keys is None:
            # **Refused here as well as at `ConsoleComposer`, and this is the layer that
            # matters.** The composer's own refusal was written first and guards the one
            # production caller; this guards the *act*. `None` defaulting to `{}` is a
            # perfectly valid "nobody reserves anything", so any future caller reaching this
            # function directly — a maintenance command, a second composition root, a debug
            # script — would build a script that silently takes a key from the agent that
            # binds it. A safety property that holds only while every caller routes through
            # one constructor is not a property of this function, and this function is where
            # the key is interpolated.
            raise ValueError(
                "a function-key forward needs the reservations to pass through; pass "
                "reserved_keys={} only to state that no provider reserves one"
            )
        for name in reserved_keys:
            if not _PROFILE_NAME.match(name):
                raise ValueError(
                    f"a profile name reaching the forwarding script is unsafe: {name!r}"
                )
        command = _forward_function_key_command(key, reserved_keys)
    elif action is ConsoleBindingAction.SHOW_PROJECTS:
        if not command:
            raise ValueError("the projects binding needs the command that returns the surface")
    elif action is ConsoleBindingAction.TOGGLE_PANES:
        if table is not ConsoleKeyTable.PREFIX:
            # The same refusal a function-key forward carries, in the other direction and for
            # the same arithmetic: every root key is argued for one at a time, and folding the
            # column is the convenience that argument does not reach. The argv is otherwise
            # identical, so a caller that asked for the root table would take a key from every
            # agent on this server and every test of the fold itself would still pass.
            raise ValueError("the panes binding may only be bound in the prefix table")
        if not command:
            raise ValueError("the panes binding needs the command that folds the column")
        command = _console_only_command(command)
    else:  # pragma: no cover - the enum has no third member
        raise ValueError(f"no argv is built for {action.value}")
    # shlex.join for /bin/sh, then `#` -> `##` for tmux's own format pass, in that order:
    # doubling first would let shlex quote the escape we just added. The doubling is also what
    # carries the forwarding script's own `#{...}` formats through to the *inner* tmux: the outer
    # pass turns `##{pane_id}` back into `#{pane_id}`, which is what the lookup needs to see.
    placement = ("-n",) if table is ConsoleKeyTable.ROOT else ("-T", table.value)
    return ("bind-key", *placement, key, "run-shell", shlex.join(command).replace("#", "##"))


def switch_client_argv(session_id: SessionId) -> tuple[str, ...]:
    """Return the full production argv that switches the current client to one session.

    The full form exists for the same reason `attach_argv` does: the one non-adapter caller
    (`adapters/tui/attach.py`, on the already-inside-our-server path) must not assemble a
    `tmux` invocation of its own — every tmux argv in the tree is codec-built (DEC-001).

    **Not the route the console uses**, and the distinction survived the tab retirement while
    its sibling did not. `switch_client_args` moved an already-attached client between
    *sessions*, which is how the console used to reach an agent, and DEC-039 records why that
    is wrong under the swap model: a session target resolves to whatever occupies the vacated
    window, so the owner lands on the projects surface rather than on the agent. This one
    serves a different caller — a surface handing back an `AttachRequest` on a host with no
    console — where the session named is the agent's own and nothing has been exchanged.
    """
    return (
        "tmux",
        "-L",
        "remote-agents",
        "switch-client",
        "-t",
        exact_session_target(f"ra-{session_id}"),
    )


def display_message_args(text: str) -> tuple[str, ...]:
    """Return the argv suffix that flashes one line on the status bar and nothing more.

    `-l` is load-bearing, not cosmetic: without it tmux format-expands the message, and
    FORMATS includes `#(shell-command)`, which tmux executes and substitutes — a status
    flash carrying session- or agent-derived text would be an arbitrary-command sink.
    With `-l` the text is printed unchanged, and `--` fences the text from the option
    parser — a message beginning with `-` is otherwise consumed as a flag (`-a` dumps the
    format table, `-c…` silently misroutes the flash). Both verified against tmux 3.4,
    2026-08-18, and pinned by the feature probe's contract test.
    """
    if not text or "\n" in text:
        raise ValueError("a status flash is exactly one non-empty line")
    return ("display-message", "-l", "--", text)


def pane_title_args(target: str) -> tuple[str, ...]:
    """Return the fixed tmux query for one already-resolved pane title.

    The format is a constant owned here, never title text supplied by the pane. The callers only
    match what comes back -- Codex's exact `Action Required` marker, and a composer's
    `busy_title` spinner for the relay -- and never retain it.
    """
    if target.startswith("%"):
        checked = exact_pane_target(target)
    else:
        checked = exact_session_target(target.removesuffix(":"))
    return ("display-message", "-p", "-t", checked, "#{pane_title}")


def console_zoom_args() -> tuple[str, ...]:
    """Return the argv suffix that prints whether the console is zoomed, and onto what.

    This replaced a read of the console's current *window* index, which was the tab model's
    proxy for "is the owner looking at the dashboard". With the tabs retired the console has
    exactly one window, so that read answered 0 forever and the status flash it guarded could
    never fire again — a rule whose premise had been deleted.

    What the question means now: the feed pane is on screen beside whatever else the owner is
    doing, so news is already visible and a flash would say it twice. The one arrangement
    where it is *not* on screen is a zoomed pane. Probed on tmux 3.4: `#{window_zoomed_flag}`
    reads `0` or `1`, and `#{pane_id}` names the active pane either way.

    The format string is this module's own fixed text, so expansion here is safe and wanted.
    """
    return (
        "display-message",
        "-p",
        "-t",
        console_target(),
        "#{window_zoomed_flag}|#{pane_id}",
    )


def console_pane_geometry_args() -> tuple[str, ...]:
    """Return the argv suffix listing every console pane's id, width, and the window's.

    One read answers both questions a slide asks -- where the split is now, and how far right
    it may go -- so the motion never issues a resize computed from a stale reading. The window
    width repeats on every line, which is tmux's shape rather than ours and costs nothing.
    """
    return (
        "list-panes",
        "-t",
        console_target(),
        "-F",
        "#{pane_id}|#{pane_width}|#{window_width}",
    )


def console_resize_pane_args(pane_id: str, width: int) -> tuple[str, ...]:
    """Return the argv suffix setting one console pane's width, in whole columns.

    Exact-targeted for the reason every pane operation here is: a resize that reaches the
    wrong pane is not a cosmetic slip, it is a live agent's window changing shape under it
    (DEC-040). `-x` takes columns rather than a percentage because the slide steps through
    measured widths, and a percentage would re-derive a different number at each step.
    """
    if width < 1:
        raise ValueError("a pane is at least one column wide")
    return ("resize-pane", "-t", exact_pane_target(pane_id), "-x", str(width))


def console_zoom_pane_args(
    pane_id: str, *, zoomed: bool, wanted: bool
) -> tuple[tuple[str, ...], ...]:
    """Return the argv suffixes that put the window into the wanted zoom state, or none.

    **`resize-pane -Z` is a toggle**, so this takes the current flag as well as the wanted
    one and issues nothing when they already agree. That is not an optimisation: the composer
    re-asserts the hidden state after every exchange, and a toggle fired unconditionally
    would unfold the column each time it was already folded.

    **Zooming selects the pane first, and the reason first written here was wrong.** That
    version claimed `-Z` leaves the active pane alone, so zooming the left slot while another
    pane was active would hide the pane the keyboard is in. Re-measured on this host's tmux
    3.4, attached and detached: `resize-pane -t %0 -Z` makes `%0` active as well as zooming
    it, and selecting another pane while zoomed auto-unzooms -- "zoomed onto a pane that is
    not selected" is not a state tmux 3.4 will hold. The select is kept because it makes the
    intent explicit and costs one argv, not because tmux needs it; it must not be removed on
    the strength of the old claim, nor kept on it.

    Unzooming issues no select: every pane is visible again, and moving the owner's cursor
    there would be a change nobody asked for.
    """
    if zoomed == wanted:
        return ()
    toggle = ("resize-pane", "-t", exact_pane_target(pane_id), "-Z")
    if not wanted:
        return (toggle,)
    return (("select-pane", "-t", exact_pane_target(pane_id)), toggle)


def console_option_args(name: str, value: str | None) -> tuple[str, ...]:
    """Return the argv suffix writing one console window option, or reading it back.

    `None` reads. `-q` on the read because an option that was never set is the ordinary case
    -- every console built before this existed -- and tmux treats asking for one as an error
    loud enough to reach a log line that would say nothing useful.
    """
    if not name.startswith("@"):
        raise ValueError("a tmux user option is namespaced with a leading @")
    if value is None:
        return ("show-options", "-w", "-q", "-v", "-t", console_target(), name)
    return ("set-option", "-w", "-t", console_target(), name, value)


#: The column count above which the bar draws its full words (handoff README § 2).
_FULL_BAR_ABOVE = 160

#: What the bar's right end says while a text entry holds the keyboard.
_TYPING_HINT = "esc cancels"


def _bar_entry(key: StatusBarKey, palette: StatusBarPalette, *, full: bool) -> str:
    """One key as the bar draws it: its number in the key colour, its words in the text colour,
    or both dim while the key would be refused."""
    words = f" {key.label}" if full else key.short
    lit = f"#[fg={palette.key}]{key.number}#[fg={palette.text}]{words}"
    dim = f"#[fg={palette.dim}]{key.number}{words}"
    if not key.bound:
        return dim
    refusals = []
    if key.needs_selection:
        refusals.append(f"#{{!=:#{{{SESSION_SELECTED_OPTION}}},1}}")
    if key.refused_while_typing:
        refusals.append(f"#{{==:#{{{TYPING_OPTION}}},1}}")
    if not refusals:
        return lit
    condition = refusals[0] if len(refusals) == 1 else f"#{{||:{refusals[0]},{refusals[1]}}}"
    return f"#{{?{condition},{dim},{lit}}}"


def _key_text(key: StatusBarKey, *, full: bool) -> str:
    return f"{key.number} {key.label}" if full else f"{key.number}{key.short}"


def _fits(width: str, beside: int) -> str:
    """A tmux condition: *width* cells (a format) fit in what is left of the row."""
    return f"#{{e|<=|:#{{e|+|:{width},{beside}}},#{{client_width}}}}"


def _first_that_fits(candidates: Sequence[tuple[str, str]], beside: int) -> str:
    """The first `(text, width)` whose width fits beside the keys, else nothing at all."""
    chosen = ""
    for text, width in reversed(candidates):
        chosen = f"#{{?{_fits(width, beside)},{text},{chosen}}}"
    return chosen


def _right_end(beside: int, *, full: bool) -> str:
    """The bar's right end: the longest whole version that fits, never a clipped one.

    Measured by the Task 2.4 live drill: tmux gives the keys priority and cuts an overlong
    right end from its *left*, so `Claude's default · codex unreachable` beside the 141-cell
    full keys drew `12 projectsol  claude …`. So each right end is offered in descending
    length -- the words, then the compact marks, then nothing -- and the first whose width
    (`#{w:…}` counts cells and skips styles) fits is drawn whole.
    """
    session = "#{session_name}"
    session_width = "#{w:session_name}"
    words = f"#{{{REMOTE_CONTROL_OPTION}}}"
    words_width = f"#{{w:{REMOTE_CONTROL_OPTION}}}"
    marks = f"#{{{REMOTE_CONTROL_COMPACT_OPTION}}}"
    marks_width = f"#{{w:{REMOTE_CONTROL_COMPACT_OPTION}}}"
    hint = str(len(_TYPING_HINT))
    if full:
        typing = [
            (f"{_TYPING_HINT}  {session}", f"#{{e|+|:{session_width},{len(_TYPING_HINT) + 2}}}"),
            (_TYPING_HINT, hint),
        ]
        reading = [
            (f"{words}  {session}", f"#{{e|+|:{words_width},#{{e|+|:{session_width},2}}}}"),
            (f"{marks}  {session}", f"#{{e|+|:{marks_width},#{{e|+|:{session_width},2}}}}"),
            (marks, marks_width),
        ]
        unread = [(session, session_width)]
    else:
        typing = [(_TYPING_HINT, hint)]
        reading = [(marks, marks_width)]
        unread = []
    # Whether a reading was published at all, asked of the option this variant draws first.
    guard = REMOTE_CONTROL_OPTION if full else REMOTE_CONTROL_COMPACT_OPTION
    published = f"#{{?#{{{guard}}},{_first_that_fits(reading, beside)}," + (
        f"{_first_that_fits(unread, beside)}}}"
    )
    return f"#{{?#{{==:#{{{TYPING_OPTION}}},1}},{_first_that_fits(typing, beside)},{published}}}"


def status_format_args(
    keys: Sequence[StatusBarKey], palette: StatusBarPalette
) -> tuple[tuple[str, ...], ...]:
    """Return the argv suffixes that give the console session its function-key bar (DEC-105).

    One status line, drawn by tmux, so it belongs to the window rather than to any pane and
    survives an agent being exchanged into the left slot (DEC-040) -- which is exactly when the
    Textual footers vanish. Built from the key table the bindings come from, never spelled.

    **Session options on the console session, not `-w` and never `-g`.** `status`,
    `status-style` and `status-format` are session options in tmux; measured on 3.4
    (`test_status_bar_probe.py`), `-w` silently lands them on the target session anyway, so
    the scope is stated rather than left to that. An agent's own `ra-<uuid>` session keeps
    tmux's default bar.

    **The width switch compares numbers.** tmux's plain `>` comparison compares strings, and
    on 3.4 it calls 99 greater than 160, so a 99-column client would get the 200-column bar.
    `e|>|` is the numeric form (R4); the stage gate greps the tree for the plain one.

    **`status-interval 0`.** A change to an option the format reads redraws the row at once
    (probed: ~7 ms), and nothing here depends on the clock, so a timed redraw buys nothing.

    **Nothing in the format shells out**, for the reason `display_message_args` records:
    `#(...)` runs a command. What the panes publish is interpolated as a value, and a value is
    never expanded again.
    """
    by_width = []
    for full in (True, False):
        drawn = [key for key in keys if key.bound or full]
        gap = "  " if full else " "
        left = gap.join(_bar_entry(key, palette, full=full) for key in drawn)
        # The keys' own width, in cells: what the right end has to fit beside.
        taken = sum(len(_key_text(key, full=full)) for key in drawn) + len(gap) * (len(drawn) - 1)
        right = _right_end(taken + len(gap), full=full)
        by_width.append(f"{left}#[align=right]#[fg={palette.muted}]{right}")
    full_bar, compact_bar = by_width
    status_format = f"#{{?#{{e|>|:#{{client_width}},{_FULL_BAR_ABOVE}}},{full_bar},{compact_bar}}}"
    target = console_target()
    return (
        ("set-option", "-t", target, "status", "on"),
        ("set-option", "-t", target, "status-position", "bottom"),
        ("set-option", "-t", target, "status-interval", "0"),
        ("set-option", "-t", target, "status-style", f"bg={palette.bar},fg={palette.text}"),
        ("set-option", "-t", target, "status-format[0]", status_format),
    )


def console_option_unset_args(name: str) -> tuple[str, ...]:
    """Return the argv suffix removing one console window option, so a reader sees it unset.

    The other half of `console_option_args` for the options the status bar reads: a pane
    that published a fact clears it when it stops knowing it, rather than leaving the bar to
    state it on the pane's behalf.
    """
    if not name.startswith("@"):
        raise ValueError("a tmux user option is namespaced with a leading @")
    return ("set-option", "-w", "-u", "-t", console_target(), name)


def _flag_args(name: str, value: bool | None) -> tuple[str, ...]:
    if value is None:
        return console_option_unset_args(name)
    return console_option_args(name, "1" if value else "0")


def session_selected_args(selected: bool | None) -> tuple[str, ...]:
    """Publish whether the sessions cursor rests on a row, for the bar's session keys.

    `None` unsets it: the bar then dims the session keys, which is the honest reading of a
    console whose sessions pane is gone.
    """
    return _flag_args(SESSION_SELECTED_OPTION, selected)


def typing_args(typing: bool | None) -> tuple[str, ...]:
    """Publish whether a text entry holds the keyboard, for the bar's stop keys and hint."""
    return _flag_args(TYPING_OPTION, typing)


#: One glyph per Remote Control tone (R6, DEC-010): the state is readable with colour off.
_REMOTE_CONTROL_GLYPHS: dict[RemoteControlTone, str] = {
    RemoteControlTone.ON: "●",
    RemoteControlTone.OFF: "○",
    RemoteControlTone.BROKEN: "?",
    RemoteControlTone.UNKNOWN: "?",
}


def _tone_colour(tone: RemoteControlTone, palette: StatusBarPalette) -> str:
    if tone is RemoteControlTone.ON:
        return palette.on
    if tone in (RemoteControlTone.OFF, RemoteControlTone.BROKEN):
        return palette.off
    return palette.dim


def _literal(text: str) -> str:
    """*text* as the status line must draw it: `#` doubled, so no `#[` or `#{` is honoured.

    An interpolated option value is not expanded again, but the draw pass still reads `#[`
    as a style and `##` as `#`. `,` and `}` need nothing: they only mean something inside a
    format being expanded, and a value is past that (both measured on 3.4).
    """
    return text.replace("#", "##")


def remote_control_words_args(
    marks: Sequence[RemoteControlMark] | None, palette: StatusBarPalette
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Publish the Remote Control readings for the bar's right end, full and compact.

    The words are the limits pane's own (DEC-084/DEC-085: the existing state words, never a
    pairing code). Each state word is coloured by its tone and every mark carries its own
    glyph, so colour is never the only signal (DEC-010). `None` unsets both values, which the
    bar reads as nothing to claim.
    """
    if marks is None:
        return (
            console_option_unset_args(REMOTE_CONTROL_OPTION),
            console_option_unset_args(REMOTE_CONTROL_COMPACT_OPTION),
        )
    rest = f"#[fg={palette.muted}]"
    words = " · ".join(
        f"{_literal(mark.provider)} #[fg={_tone_colour(mark.tone, palette)}]"
        f"{_literal(mark.word)}{rest}"
        for mark in marks
    )
    glyphs = "".join(
        f"#[fg={_tone_colour(mark.tone, palette)}]{_REMOTE_CONTROL_GLYPHS[mark.tone]}"
        for mark in marks
    )
    return (
        console_option_args(REMOTE_CONTROL_OPTION, f"Remote Control  {words}"),
        console_option_args(REMOTE_CONTROL_COMPACT_OPTION, f"RC {glyphs}{rest}"),
    )


#: The tmux **server** options this project may set, by name. An allowlist rather than a
#: prefix rule because these are tmux's own names: `@` protects `console_option_args` above,
#: and there is no syntax that distinguishes a server option we own from one we do not.
#:
#: `mouse` earns its place (BL-098). With tmux's default `off`, tmux enables terminal mouse
#: reporting only for the **active** pane's application -- and the console's resting state is
#: an agent displayed in the left slot. An agent that does not ask for mouse (codex reports
#: `mouse_any_flag=0`) therefore means tmux never asks the terminal to report mouse at all,
#: so clicking a surface pane does nothing. Measured on the owner's host 2026-09-17: the
#: three surface panes report `mouse_any_flag=1` -- they want it -- and the active agent
#: pane `0`.
#:
#: The accepted cost, recorded because it is real and is why tmux does not default to it:
#: native terminal text selection then needs Shift held, and the wheel enters copy-mode in
#: panes that do not request mouse. It is accepted on **this project's own socket only**.
_OWNED_SERVER_OPTIONS = frozenset({"mouse"})


def console_server_option_args(name: str, value: str) -> tuple[str, ...]:
    """Return the argv suffix setting one **global session** option on our own server.

    `-g`, not `-w` or `-p`: `mouse` is a session option, and the console and the `ra-<uuid>`
    sessions an agent launch creates share one server. Setting it globally is what makes the
    behaviour the same whichever of them the owner is looking at.

    **No target.** Every command this adapter builds already runs through
    `("tmux", "-L", <our socket>)`, so the write cannot reach another server -- which is the
    property `test_console_mouse.py` proves against a real one rather than asserting here.

    The allowlist refusal keeps `set -g` from becoming a general escape hatch: it is the
    widest write in this codec, and a later caller reaching for it to set `prefix` or
    `default-shell` would be reconfiguring a server the owner did not ask us to touch.
    """
    if name not in _OWNED_SERVER_OPTIONS:
        raise ValueError(
            f"{name} is not a server option this project owns; "
            f"owned: {', '.join(sorted(_OWNED_SERVER_OPTIONS))}"
        )
    return ("set-option", "-g", name, value)


def console_slot_mark_args(
    pane_id: str, slot: ConsolePaneSlot = ConsolePaneSlot.PROJECTS
) -> tuple[str, ...]:
    """Return the argv suffix that marks one pane as one of the console's three.

    `-p`, for the same reason identity is pane-scoped: the mark has to travel with the pane
    an exchange sends into an agent's window, because finding it there again is the entire
    job. Marked on the console *session* it would stay behind and describe whatever swapped
    in — the failure mode DEC-038 records for identity, in a second vocabulary.

    What it buys is exactness. Without it the parked surface is identified as "the only pane
    in that window carrying no identity", which stops being an answer the moment an operator
    splits the agent's window: two candidates, no way to choose, and a console with no route
    back to its own surface.
    """
    return (
        "set-option",
        "-p",
        "-t",
        exact_pane_target(pane_id),
        CONSOLE_SLOT_OPTION,
        slot.value,
    )


def publish_selection_args(session_id: SessionId | None) -> tuple[str, ...]:
    """Return the argv suffix publishing which session the console has selected.

    `-t` and the console session, not `-p` and a pane: see `SELECTED_SESSION_OPTION` for why a
    selection is console state rather than pane identity, and why DEC-038 does not reach it.

    `None` writes the **empty string** rather than unsetting the option. The sessions pane
    clears its cursor whenever the highlighted row leaves the list (DEC-052, DEC-062), and that
    has to be published: an option left naming a row that has gone is exactly the stale
    selection a key in another pane would then act on. Empty is also what `show-options -qv`
    returns for an option never set, so "cleared" and "never written" decode identically by
    construction rather than by two readers agreeing to.
    """
    return (
        "set-option",
        "-t",
        console_target(),
        SELECTED_SESSION_OPTION,
        "" if session_id is None else str(session_id),
    )


def read_selection_args() -> tuple[str, ...]:
    """Return the argv suffix reading the published selection back.

    `-q` so an unset option is the empty string rather than an error, and `-v` so the value
    arrives alone rather than as `name value` — the two together make "nothing is selected" a
    value this can decode instead of a failure it would have to interpret.
    """
    return ("show-options", "-qv", "-t", console_target(), SELECTED_SESSION_OPTION)


def decode_selection(raw: str) -> SessionId | None:
    """Decode a published selection, refusing anything that is not a session id.

    Refusing is the only safe answer. What this returns is what a session key acts on, and one
    of those keys ends a session with no confirmation (DEC-018), so a value this process did
    not write — a hand-set option, a truncated read, a leftover from a tmux the owner drives
    themselves — must decode to "nothing selected" rather than to something addressable.

    DEC-007 is the second half of that and is unchanged: the acting surface re-reads the record
    and re-checks `available_actions` at issue time, so even a well-formed id that names a
    session the policy now forbids cannot be acted on.
    """
    value = raw.strip()
    if not value:
        return None
    try:
        return SessionId.parse(value)
    except ValueError:
        return None


def console_layout_args(main_percent: int, column: Sequence[tuple[str, int]]):
    """Return the argv suffixes that put the console window back in its proportions.

    Needed only after a **rebuild**, and only because a rebuilt pane inherits the shape of
    whatever it was split from rather than the shape it is meant to have. Measured on tmux
    3.4 at 80x24: when the projects pane dies its space goes to the right-hand column, so
    splitting the sessions pane to bring it back leaves projects a 48x16 box in the top-left
    and the feed running the *full width* underneath both — correct marks, correct side, and
    the wrong window. `-b` puts the pane on the right side of the sessions pane; it cannot
    undo a layout tree that changed while the pane was missing.

    So the tree is rebuilt rather than nudged. `main-vertical` is exactly this layout — one
    full-height pane on the left, the rest stacked on the right — and it divides the right
    column *evenly*, which is why `column` resizes it afterwards.

    `column` is ordered, top pane first, and it names **fewer panes than the column holds**:
    the last pane's height is what the named ones leave. That is not tidiness, it is what
    `resize-pane -y` does. Probed on 3.4 at 183x44 against the three-pane column this console
    now has (sessions, limits, feed), from an even 14/14/14:

    - a resize of the **last** pane works against the pane *above* it, so feed → 33% is a
      no-op at three panes and the column stays even. That is the defect this argument
      replaces: with only the feed named, the limits pane kept a third of the column and the
      sessions list was left with 14 rows out of 42.
    - a resize of a pane that is *not* last takes its rows from the ones below, so sessions
      → 41% then feed → 33% lands 18/10/14 — the same shape a fresh build produces, which is
      the whole point of this call.

    `-y` is a share of the **window** height, where a split's `-l` is a share of the pane
    being split. The two numbers describing one pane are therefore different numbers.
    """
    if not 1 <= main_percent <= 99:
        raise ValueError("a console layout takes percentages strictly inside 0 and 100")
    if any(not 1 <= percent <= 99 for _, percent in column):
        raise ValueError("a console layout takes percentages strictly inside 0 and 100")
    return (
        ("set-window-option", "-t", console_target(), "main-pane-width", f"{main_percent}%"),
        ("select-layout", "-t", console_target(), "main-vertical"),
        *(
            ("resize-pane", "-t", exact_pane_target(pane_id), "-y", f"{percent}%")
            for pane_id, percent in column
        ),
    )


def split_console_pane_args(
    target_pane: str,
    command: tuple[str, ...],
    cwd: Path,
    *,
    vertical: bool,
    percent: int,
    before: bool = False,
) -> tuple[str, ...]:
    """Return the argv suffix that splits one console pane and runs a command in the new one.

    `-l <percent>%` rather than `-p <percent>`: **tmux 3.4 removed `-p`**, and it fails with
    `size missing` rather than falling back to a default — probed on a disposable socket
    rather than read off the manual, because a layout that silently came out even would look
    like a rounding difference rather than a rejected flag. `-l` sizes the **new** pane, which
    is what the percentages in the layout mean. Measured at 200x50: `-l 40%` gives 119 and 80
    columns, and `-l 33%` on the right-hand pane gives 33 and 16 rows.

    `-d` keeps focus where it was. Without it every split makes its own new pane current, so
    building the window would leave the owner's keyboard resting in the feed.

    `-P -F '#{pane_id}'` makes tmux name the pane it just created. The alternative — list the
    window afterwards and take the last row — is a guess the moment anything else splits, and
    the id is what the next split and the slot mark both need.

    `-b` puts the new pane **before** its target rather than after. It exists for one case:
    rebuilding the projects pane after its process died. That pane is normally the one the
    window was created with, so there is nothing to its left to split off — the only pane
    left to split from is the sessions pane to its right, and without `-b` the rebuilt
    surface would appear on the wrong side of the console.
    """
    if not command:
        raise ValueError("a console pane needs a command to run")
    if not 1 <= percent <= 99:
        raise ValueError("a console pane split takes a percentage strictly inside 0 and 100")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("console working directory must be an existing absolute directory")
    return (
        "split-window",
        "-v" if vertical else "-h",
        "-d",
        *(("-b",) if before else ()),
        "-t",
        exact_pane_target(target_pane),
        "-l",
        f"{percent}%",
        "-c",
        str(cwd),
        "-P",
        "-F",
        "#{pane_id}",
        *command,
    )


def rejoin_console_pane_args(
    pane_id: str,
    beside_pane: str,
    *,
    vertical: bool,
    percent: int,
    before: bool = False,
) -> tuple[str, ...]:
    """Return the argv suffix that moves one pane into the window another pane is in.

    `join-pane` rather than `swap-pane`, and the two are not interchangeable: a swap trades,
    so it needs a partner worth having on the far end. This is for the case where there is
    none — the window the console's pane was parked in has had its agent destroyed — and
    trading there would just send a second console pane out to take its place.

    The flags mirror `split_console_pane_args` on purpose, because this fills the position
    that function would have built: `-h`/`-v` for the axis, `-l <percent>%` sizing the pane
    being moved in, `-b` to put it *before* its neighbour, and `-d` to keep focus where it
    was rather than following the pane. Probed on tmux 3.4 rather than read off the manual:
    `join-pane -b -h -l 60% -s <surface> -t <sessions>` against a console reduced to two
    panes restored it to three, and the emptied window took its defunct session with it. The
    geometry afterwards was 108x29/71x29/180x14 -- correct order, wrong shape, because the
    layout tree changed while the pane was away. That is `console_layout_args`' job and the
    same one it already does after a rebuild; run after it, the three panes measured
    107x44/72x29/72x14, which is a fresh build to the column.
    """
    if not 1 <= percent <= 99:
        raise ValueError("a console pane rejoin takes a percentage strictly inside 0 and 100")
    return (
        "join-pane",
        "-v" if vertical else "-h",
        "-d",
        *(("-b",) if before else ()),
        "-l",
        f"{percent}%",
        "-s",
        exact_pane_target(pane_id),
        "-t",
        exact_pane_target(beside_pane),
    )


def list_arrangement_args() -> tuple[str, ...]:
    """Return the argv suffix that lists every pane on the server with its position.

    Server-wide rather than console-scoped, and that is the point: an exchange leaves one
    pane in the console and its partner parked in a managed session's own window, so a read
    that saw only the console could say what is displayed and never where the displaced pane
    went. One listing answers both, and no session target appears in it — the composer never
    names `ra-<uuid>:` to ask about a session's window, it filters a listing it already has.
    """
    return ("list-panes", "-a", "-F", ARRANGEMENT_FORMAT)


def parse_arrangement(
    line: str,
) -> tuple[SessionId | None, bool, int, int, str, SessionId | None, bool, str | None]:
    """Decode one line into (host, on console, window, position, pane, identity, surface).

    Two decodings, and keeping them apart is the whole job. **Host** comes from the session
    name the pane is *listed under* — the console, a managed session, or neither — and says
    where the pane is being shown. **Identity** comes from the pane's own schema-2 mark and
    says whose it is. Under the swap model those disagree exactly when something is displaced,
    which is the state the composer exists to read.

    A schema-1 mark is never returned as identity. tmux resolves `#{@option}` by falling back
    pane -> session, so every pane in a legacy session's window reports that session's id
    whether or not it is the agent; treating that as identity would make the surface parked in
    such a window look like the agent itself. What it does say — which session's window this
    pane sits in — is what `host` already answers.

    Refuses rather than guesses. A session name containing the format delimiter inflates the
    split past seven fields (tmux 3.4 accepts `|` in a session name — Claim 3), and an
    unparseable position is not a position; both raise, and the gateway drops the line.
    """
    fields = line.rstrip("\n").split(_DELIMITER)
    if len(fields) != 7:
        raise ValueError("arrangement format has missing fields")
    name, raw_window, raw_pane_index, pane_id, schema, raw_id, slot = fields
    try:
        window_index, pane_index = int(raw_window), int(raw_pane_index)
    except ValueError as error:
        raise ValueError("arrangement position is invalid") from error
    if window_index < 0 or pane_index < 0:
        raise ValueError("arrangement position is invalid")
    if not pane_id:
        raise ValueError("arrangement format has missing fields")
    on_console = name == CONSOLE_SESSION_NAME
    host: SessionId | None = None
    if not on_console and name.startswith("ra-"):
        try:
            host = SessionId.parse(name.removeprefix("ra-"))
        except ValueError:
            host = None
    identity = pane_owned_identity(schema, raw_id)
    return (
        host,
        on_console,
        window_index,
        pane_index,
        pane_id,
        identity,
        slot == SURFACE_SLOT,
        slot or None,
    )


def is_console_view(line: str) -> bool:
    """Say whether one list-panes line is the console's view rather than evidence.

    The console reports its own dashboard pane and re-reports every linked window under its
    own name (tmux 3.4, verified). A console line is presentation exactly when it carries no
    managed mark — which is the narrow reading, and it has to be narrow now: under the swap
    model a managed pane can be *hosted* by the console, and that line is the agent itself.
    Dropping it because of the name it is listed under would report a running session as gone.

    tmux 3.4 accepts `|` inside a session name (verified 2026-08-18, pinned by the feature
    probe's contract test), and the pane format uses `|` as its delimiter, so a stray
    session named e.g. `ra-console|x` would mis-split into a line whose *first field* reads
    `ra-console`. The field-count check keeps such an impostor out of this drop: its
    embedded delimiter inflates the split past the format's ten fields, so it falls through
    to `parse_pane` and is quarantined as orphan evidence — exactly where a stray session's
    line always went. The empty-schema check carries the rest: a console *view* has no mark
    of its own and the console session sets none, so a blank schema field is what makes a
    line presentation. A ten-field `ra-console` line that does carry a mark falls through to
    `parse_pane`, which then decides on the schema — a pane-scoped schema-2 mark is a real
    displaced agent and decodes, while a schema-1 mark under this name cannot be the session
    it names and is quarantined.
    """
    fields = line.rstrip("\n").split(_DELIMITER)
    return len(fields) == 10 and fields[0] == CONSOLE_SESSION_NAME and fields[6] == ""


def parse_pane(line: str) -> ManagedPane:
    """Decode one managed tmux pane or refuse ambiguous and untrusted metadata."""
    fields = line.rstrip("\n").split(_DELIMITER)
    if len(fields) != 10:
        raise ValueError("tmux pane format has missing fields")
    (
        name,
        _tmux_session_id,
        pane_id,
        raw_pid,
        pane_dead,
        _dead_status,
        schema,
        raw_id,
        project,
        profile,
    ) = fields
    if schema not in _DECODABLE_SCHEMA_VERSIONS:
        raise ValueError("tmux management schema is missing or unsupported")
    if any(not field for index, field in enumerate(fields) if index not in {4, 5}):
        raise ValueError("tmux pane format has missing fields")
    session_id = SessionId.parse(raw_id)
    # A schema-1 mark on a line under another name is *inherited*, never identity: the
    # session it belongs to still answers for panes that merely occupy its window. Schema 2
    # is stamped on the pane itself, so it stays true wherever tmux lists the pane.
    if schema == _SCHEMA_VERSION and name != f"ra-{session_id}":
        raise ValueError("managed session name does not match its opaque identifier")
    if pane_dead not in {"0", "1"}:
        raise ValueError("tmux pane-dead field is invalid")
    try:
        process_id = int(raw_pid)
    except ValueError as error:
        raise ValueError("tmux pane PID is invalid") from error
    if process_id <= 1:
        raise ValueError("tmux pane PID is invalid")
    return ManagedPane(
        name,
        pane_id,
        pane_owned_identity(schema, raw_id) is not None,
        session_id,
        ProjectId(project),
        ProfileId(_RETIRED_PROFILE_IDS.get(profile, profile)),
        process_id,
        live=pane_dead == "0",
        preserved=pane_dead == "1",
    )
