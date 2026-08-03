"""Pinned tmux 3.4 pane format and strict managed-session decoding."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_DELIMITER = "|"
_SCHEMA_VERSION = "1"
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


def attach_command(session_id: SessionId) -> str:
    """Return the one copyable attach command for a currently verified managed session."""
    return f"tmux -L remote-agents attach-session -t {exact_session_target(f'ra-{session_id}')}"


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
