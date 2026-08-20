"""Pinned tmux 3.4 pane format and strict managed-session decoding."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.console import ConsoleBindingAction, ConsolePaneSlot

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

# Window-index-to-owning-session mapping for the console. The mark below is set
# *window-scoped* on the source (see `window_session_mark_args`) because a linked window is
# one shared object: a window option travels with it into the console's listing, while the
# managed session's session-scoped options do not (verified against tmux 3.4, 2026-08-18).
WINDOW_SESSION_OPTION = "@remote_agents_window_session"

# The console's own surface pane, marked so recovery can find it wherever an exchange parked
# it. Deliberately *not* one of the four identity options above and not part of that
# vocabulary: it says "this pane belongs to the console", never "this pane is a session".
# Pane-scoped, so it travels with the surface exactly as identity travels with an agent — and
# so nothing inherits it, since neither the console session nor a managed one sets it.
CONSOLE_SLOT_OPTION = "@remote_agents_console_slot"
SURFACE_SLOT = "surface"

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
CONSOLE_WINDOW_FORMAT = _DELIMITER.join(("#{window_index}", f"#{{{WINDOW_SESSION_OPTION}}}"))
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
    """
    target = exact_session_target(f"ra-{session_id}")
    return tuple(
        ("set-option", "-p", "-t", target, option, value)
        for option, value in (
            (_SCHEMA_OPTION, _PANE_SCHEMA_VERSION),
            (_ID_OPTION, str(session_id)),
            (_PROJECT_OPTION, str(project_id)),
            (_PROFILE_OPTION, str(profile_id)),
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


def link_window_args(session_id: SessionId) -> tuple[str, ...]:
    """Return the argv suffix that links one managed session's window into the console.

    The bare destination `ra-console:` appends at the next free index (tmux 3.4 behavior,
    verified on a disposable socket rather than assumed), so the builder never has to guess
    an index that another link may have taken between the listing and the call. `-d` is
    load-bearing: without it tmux makes the new window current, so every background sync
    that linked a tab yanked the console away from whatever the owner was doing — observed
    on the first live drive against real sessions, invisible to every headless test.
    """
    return (
        "link-window",
        "-d",
        "-s",
        exact_session_target(f"ra-{session_id}"),
        "-t",
        console_target(),
    )


def window_session_mark_args(session_id: SessionId) -> tuple[str, ...]:
    """Return the argv suffix that marks a managed session's window with its own identity.

    `-w` is the point: the mark must live on the *window*, which is the object `link-window`
    shares with the console, not on the session, whose options stay home.
    """
    return (
        "set-option",
        "-w",
        "-t",
        exact_session_target(f"ra-{session_id}"),
        WINDOW_SESSION_OPTION,
        str(session_id),
    )


def unlink_window_args(window_index: int) -> tuple[str, ...]:
    """Return the argv suffix that unlinks one console tab; the dashboard is not a tab."""
    if window_index < 1:
        raise ValueError("only linked console tabs may be unlinked, never the dashboard")
    return ("unlink-window", "-t", f"{CONSOLE_SESSION_NAME}:{window_index}")


#: Which characters a bindable key may be made of, once one optional modifier is stripped.
_BINDABLE_KEY_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


def console_binding_args(
    key: str, action: ConsoleBindingAction, command: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Return the argv suffix that installs one console root binding, on our socket only.

    `-n` is the root table: no prefix, which is the whole reason these keys cost something —
    a root binding is a key every agent on this server can never receive, for as long as it
    is bound. That is why the key is validated here rather than trusted, and why the *set* of
    them is declared in one place in the application layer rather than accumulated.

    The two actions differ in shape, and the mismatch is refused rather than ignored:
    `SHOW_PROJECTS` needs a command to run and `FOCUS_NEXT_PANE` must not carry one, so a
    caller that passes the wrong pair gets a `ValueError` instead of a binding that quietly
    does nothing.

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

    Today's only caller passes a fixed tuple built from `sys.executable`, so nothing
    owner-controlled reaches this — which is why it is escaped now, while it is cheap, rather
    than when a future binding is built from a project path or a profile name and reintroduces
    the class silently.
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
    if action is ConsoleBindingAction.SHOW_PROJECTS:
        if not command:
            raise ValueError("the projects binding needs the command that returns the surface")
        # shlex.join for /bin/sh, then `#` -> `##` for tmux's own format pass, in that order:
        # doubling first would let shlex quote the escape we just added.
        return ("bind-key", "-n", key, "run-shell", shlex.join(command).replace("#", "##"))
    if command:
        raise ValueError(f"{action.value} takes no command; tmux does this one on its own")
    # `:.+` is "the next pane of the current window", cycling at the end. Relative to whatever
    # window the client pressing the key is on — the console's when it matters, and otherwise
    # a managed session's own window, where it moves focus if that window has more than one
    # pane. That is not nothing: this project treats an operator's hand-split pane as ordinary
    # (see `inventory`'s inherited-mark handling), and the surface hands out an attach command
    # for exactly that kind of direct connection. Harmless — focus moves, nothing else — but
    # "a no-op outside the console" would be a false claim, so it is not made.
    return ("bind-key", "-n", key, "select-pane", "-t", ":.+")


def select_window_args(window_index: int) -> tuple[str, ...]:
    """Return the argv suffix that focuses one console window, index 0 being the dashboard."""
    if window_index < 0:
        raise ValueError("a console window index is never negative")
    return ("select-window", "-t", f"{CONSOLE_SESSION_NAME}:{window_index}")


def switch_client_args(session_id: SessionId) -> tuple[str, ...]:
    """Return the argv suffix that moves the attached client to one exact managed session."""
    return ("switch-client", "-t", exact_session_target(f"ra-{session_id}"))


def switch_client_argv(session_id: SessionId) -> tuple[str, ...]:
    """Return the full production argv that switches the current client to one session.

    The full form exists for the same reason `attach_argv` does: the one non-adapter caller
    (`adapters/tui/attach.py`, on the already-inside-our-server path) must not assemble a
    `tmux` invocation of its own — every tmux argv in the tree is codec-built (DEC-001).
    """
    return ("tmux", "-L", "remote-agents", *switch_client_args(session_id))


def switch_client_console_args() -> tuple[str, ...]:
    """Return the argv suffix that moves the attached client back to the console."""
    return ("switch-client", "-t", console_target())


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


def current_console_window_args() -> tuple[str, ...]:
    """Return the argv suffix that prints the console session's current window index.

    A proxy for "is the owner looking at the dashboard": the console's current window is
    0 exactly when its client rests on the dashboard tab. The format string is this
    module's own fixed text, so expansion here is safe and wanted.
    """
    return ("display-message", "-p", "-t", console_target(), "#{window_index}")


def list_console_windows_args() -> tuple[str, ...]:
    """Return the argv suffix that lists console windows in the pinned mapping format."""
    return ("list-windows", "-t", console_target(), "-F", CONSOLE_WINDOW_FORMAT)


def parse_console_window(line: str) -> tuple[int, SessionId | None]:
    """Decode one console window line into (index, owning session or None for unmarked)."""
    fields = line.rstrip("\n").split(_DELIMITER)
    if len(fields) != 2:
        raise ValueError("console window format has missing fields")
    raw_index, raw_session = fields
    try:
        index = int(raw_index)
    except ValueError as error:
        raise ValueError("console window index is invalid") from error
    if index < 0:
        raise ValueError("console window index is invalid")
    if not raw_session:
        return (index, None)
    return (index, SessionId.parse(raw_session))


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
        ProfileId(profile),
        process_id,
        live=pane_dead == "0",
        preserved=pane_dead == "1",
    )
