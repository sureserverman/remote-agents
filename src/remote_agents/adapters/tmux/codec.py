"""Pinned tmux 3.4 pane format and strict managed-session decoding."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_DELIMITER = "|"
_SCHEMA_VERSION = "1"

# The console session carries the `ra-` prefix so it visibly belongs to this socket, and a
# non-UUID suffix so `exact_session_target` can never accept it: no lifecycle code path can
# address the console as a managed session by construction rather than by discipline.
CONSOLE_SESSION_NAME = "ra-console"

# Window-index-to-owning-session mapping for the console. `@remote_agents_window_session` is
# set *window-scoped* on the source (see `window_session_mark_args`) because a linked window
# is one shared object: a window option travels with it into the console's listing, while the
# managed session's session-scoped options do not (verified against tmux 3.4, 2026-08-18).
CONSOLE_WINDOW_FORMAT = _DELIMITER.join(("#{window_index}", "#{@remote_agents_window_session}"))
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


def link_window_args(session_id: SessionId) -> tuple[str, ...]:
    """Return the argv suffix that links one managed session's window into the console.

    The bare destination `ra-console:` appends at the next free index (tmux 3.4 behavior,
    verified on a disposable socket rather than assumed), so the builder never has to guess
    an index that another link may have taken between the listing and the call.
    """
    return (
        "link-window",
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
        "@remote_agents_window_session",
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


def switch_client_console_args() -> tuple[str, ...]:
    """Return the argv suffix that moves the attached client back to the console."""
    return ("switch-client", "-t", console_target())


def display_message_args(text: str) -> tuple[str, ...]:
    """Return the argv suffix that flashes one line on the status bar and nothing more."""
    if not text or "\n" in text:
        raise ValueError("a status flash is exactly one non-empty line")
    return ("display-message", text)


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
    own name (tmux 3.4, verified). Both describe presentation, not sessions: the linked
    duplicate's pane is already listed — with its options intact — under its home session.
    """
    return line.split(_DELIMITER, 1)[0] == CONSOLE_SESSION_NAME


def parse_pane(line: str) -> ManagedPane:
    """Decode one managed tmux pane or refuse ambiguous and untrusted metadata."""
    fields = line.rstrip("\n").split(_DELIMITER)
    if len(fields) != 10:
        raise ValueError("tmux pane format has missing fields")
    (
        name,
        _tmux_session_id,
        _pane_id,
        raw_pid,
        pane_dead,
        _dead_status,
        schema,
        raw_id,
        project,
        profile,
    ) = fields
    if schema != _SCHEMA_VERSION:
        raise ValueError("tmux management schema is missing or unsupported")
    if any(not field for index, field in enumerate(fields) if index not in {4, 5}):
        raise ValueError("tmux pane format has missing fields")
    session_id = SessionId.parse(raw_id)
    if name != f"ra-{session_id}":
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
        session_id,
        ProjectId(project),
        ProfileId(profile),
        process_id,
        live=pane_dead == "0",
        preserved=pane_dead == "1",
    )
