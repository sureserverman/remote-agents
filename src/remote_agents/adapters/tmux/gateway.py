"""Dedicated-socket tmux inventory and ownership boundary."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from remote_agents.adapters.tmux.codec import (
    PANE_FORMAT,
    ManagedPane,
    exact_session_target,
    parse_pane,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId


class TmuxRunner(Protocol):
    """Argument-vector subprocess boundary used by the tmux adapter."""

    async def run(self, *argv: str) -> str: ...


@dataclass(frozen=True, slots=True)
class OrphanEvidence:
    """Read-only evidence for a pane that cannot be trusted as managed."""

    raw: str
    reason: str


@dataclass(frozen=True, slots=True)
class TmuxInventory:
    """Trusted managed panes and quarantined evidence from one dedicated server."""

    managed: tuple[ManagedPane, ...]
    orphans: tuple[OrphanEvidence, ...]


class TmuxGateway:
    """Forbid default-server and broad-target paths before subprocess execution."""

    def __init__(
        self,
        socket_name: str,
        runner: TmuxRunner,
        *,
        intent_directory: Path = Path("/var/lib/remote-agents/intents"),
    ) -> None:
        if socket_name != "remote-agents" and not socket_name.startswith("remote-agents-test-"):
            raise ValueError("a dedicated socket name is required")
        self._socket_name = socket_name
        self._runner = runner
        self._intent_directory = intent_directory

    async def inventory(self) -> TmuxInventory:
        """List panes only on the dedicated socket and quarantine malformed tags."""
        output = await self._runner.run(*self._base_argv(), "list-panes", "-a", "-F", PANE_FORMAT)
        managed: list[ManagedPane] = []
        orphans: list[OrphanEvidence] = []
        for line in output.splitlines():
            if not line:
                continue
            try:
                managed.append(parse_pane(line))
            except ValueError as error:
                orphans.append(OrphanEvidence(line, str(error)))
        return TmuxInventory(tuple(managed), tuple(orphans))

    async def mutate(self, operation: str, session_name: str) -> str:
        """Run the one supported destructive operation against an exact managed target."""
        if operation != "kill-session":
            raise ValueError("forbidden tmux operation")
        return await self._runner.run(
            *self._base_argv(), operation, "-t", exact_session_target(session_name)
        )

    async def capture(self, session_id: SessionId) -> str:
        """Capture only one exact managed pane without tmux escape-sequence output."""
        return await self._runner.run(
            *self._base_argv(), "capture-pane", "-p", "-t", exact_session_target(f"ra-{session_id}")
        )

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId, cwd: Path
    ) -> None:
        """Create a tagged managed session that invokes only the fixed runner module."""
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("launch working directory must be an existing absolute directory")
        session_name = f"ra-{session_id}"
        target = exact_session_target(session_name)
        await self._runner.run(
            *self._base_argv(),
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(cwd),
            sys.executable,
            "-m",
            "remote_agents.adapters.tmux.session_runner",
            str(session_id),
            "--intent-dir",
            str(self._intent_directory),
        )
        await self._runner.run(
            *self._base_argv(), "set-option", "-t", target, "remain-on-exit", "on"
        )
        for option, value in (
            ("@remote_agents_schema", "1"),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", str(project_id)),
            ("@remote_agents_profile", str(profile_id)),
        ):
            await self._runner.run(*self._base_argv(), "set-option", "-t", target, option, value)

    def _base_argv(self) -> tuple[str, str, str]:
        """Return the only valid tmux server selector for this adapter."""
        return ("tmux", "-L", self._socket_name)

    @property
    def intent_directory(self) -> Path:
        """Return the adapter-owned private directory supplied to the fixed runner."""
        return self._intent_directory
