"""Pinned tmux 3.4 pane format and strict managed-session decoding."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.models import ProfileId, ProjectId, SessionId

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
CONSOLE_WINDOW_FORMAT = _DELIMITER.join(("#{window_index}", f"#{{{WINDOW_SESSION_OPTION}}}"))
PANE_FORMAT = _DELIMITER.join(
    (
        "#{session_name}",
        "#{session_id}",
        "#{pane_id}",
        "#{pane_pid}",
        "#{pane_dead}",
        "#{pane_dead_status}",
        "#{@remote_agents_schema}",
        "#{@remote_agents_id}",
        "#{@remote_agents_project_id}",
        "#{@remote_agents_profile}",
    )
)


@dataclass(frozen=True, slots=True)
class ManagedPane:
    """Trusted tmux metadata decoded from the pinned format-version contract."""

    session_name: str
    """The session *hosting* this pane, which is only the managed session's own name under
    schema 1. A schema-2 pane keeps its identity wherever tmux lists it, so this field
    answers "who is showing it" and never "which session is it" — build a lifecycle target
    from `session_id`, never from this."""

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
    both windows non-empty, which is the whole reason this design is swap-based rather than
    the `join-pane` shape that emptied a managed session's window and destroyed the session
    with it (probed 2026-08-19; DEC-036's rejected Shape B).
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
            ("@remote_agents_schema", _PANE_SCHEMA_VERSION),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", str(project_id)),
            ("@remote_agents_profile", str(profile_id)),
        )
    )


def attach_argv(session_id: SessionId, *, read_only: bool = False) -> tuple[str, ...]:
    """Return the exact argument vector that attaches to one managed session.

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
        exact_session_target(f"ra-{session_id}"),
    )


def attach_command(session_id: SessionId, *, read_only: bool = False) -> str:
    """Return the one copyable attach command for a currently verified managed session."""
    return " ".join(attach_argv(session_id, read_only=read_only))


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
        schema == _PANE_SCHEMA_VERSION,
        session_id,
        ProjectId(project),
        ProfileId(profile),
        process_id,
        live=pane_dead == "0",
        preserved=pane_dead == "1",
    )
