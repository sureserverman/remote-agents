"""Map exact frozen destinations to one current registered project."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path

from remote_agents.domain.models import ProjectId
from remote_agents.ports.handoff_envelopes import HandoffDestination
from remote_agents.ports.rollover_store import ResolvedRolloverDestination
from remote_agents.ports.worktree_store import WorktreeStore


class DestinationRefused(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RolloverDestinations:
    def __init__(
        self,
        projects: Mapping[ProjectId, Path],
        inspect_branch: Callable[[Path], Awaitable[str | None]],
        worktrees: WorktreeStore | None = None,
    ) -> None:
        self._projects = projects
        self._inspect = inspect_branch
        self._worktrees = worktrees

    async def resolve(self, destination: HandoffDestination) -> ResolvedRolloverDestination:
        root = destination.root
        candidates = []
        for project, registered in tuple(self._projects.items()):
            if registered.resolve() == root:
                candidates.append(project)
            if self._worktrees is not None:
                for worktree in await self._worktrees.live_for_project(project):
                    if worktree.path.resolve() == root:
                        candidates.append(project)
        if not candidates:
            raise DestinationRefused("destination-unregistered")
        if len(candidates) != 1:
            raise DestinationRefused("ambiguous-destination")
        branch = await self._inspect(root)
        if branch is None:
            raise DestinationRefused("destination-root")
        if destination.branch and branch != destination.branch:
            raise DestinationRefused("branch-mismatch")
        return ResolvedRolloverDestination(candidates.pop(), replace(destination, branch=branch))
