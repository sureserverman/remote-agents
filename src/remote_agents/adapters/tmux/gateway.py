"""Dedicated-socket tmux inventory and ownership boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from remote_agents.adapters.tmux.codec import (
    PANE_FORMAT,
    ManagedPane,
    exact_session_target,
    parse_pane,
)


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

    def __init__(self, socket_name: str, runner: TmuxRunner) -> None:
        if socket_name != "remote-agents" and not socket_name.startswith("remote-agents-test-"):
            raise ValueError("a dedicated socket name is required")
        self._socket_name = socket_name
        self._runner = runner

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

    def _base_argv(self) -> tuple[str, str, str]:
        """Return the only valid tmux server selector for this adapter."""
        return ("tmux", "-L", self._socket_name)
