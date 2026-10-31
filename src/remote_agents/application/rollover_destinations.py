"""Map exact frozen destinations to one current registered project."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path

from remote_agents.application.session_folders import live_folder
from remote_agents.domain.models import ProjectId
from remote_agents.ports.handoff_envelopes import HandoffDestination
from remote_agents.ports.rollover_store import ResolvedRolloverDestination
from remote_agents.ports.worktree_store import Worktree, WorktreeStore


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
        *,
        refresh: Callable[[Path], Awaitable[None]] | None = None,
    ) -> None:
        self._projects = projects
        self._inspect = inspect_branch
        self._worktrees = worktrees
        self._refresh = refresh

    async def resolve(self, destination: HandoffDestination) -> ResolvedRolloverDestination:
        try:
            root = destination.root
            candidates = await self._candidates(root)
            if not candidates and self._refresh is not None:
                # A project made since the catalogue was last read is not unregistered.
                await self._refresh(root)
                candidates = await self._candidates(root)
            if not candidates:
                raise DestinationRefused("destination-unregistered")
            if len(candidates) != 1:
                raise DestinationRefused("ambiguous-destination")
            project, worktree = candidates[0]
            if worktree is not None and live_folder(worktree) is None:
                raise DestinationRefused("destination-root")
            branch = await self._inspect(root)
            if branch is None:
                raise DestinationRefused("destination-root")
            if destination.branch and branch != destination.branch:
                raise DestinationRefused("branch-mismatch")
            return ResolvedRolloverDestination(project, replace(destination, branch=branch))
        except (OSError, RuntimeError) as error:
            # Path.resolve raises on unavailable paths on supported Python3.12; newer
            # runtimes may instead return a path the Git inspector refuses. Both are
            # input refusals, so neither may disappear into the pass's retry logger.
            raise DestinationRefused("destination-root") from error

    async def _candidates(self, root: Path) -> list[tuple[ProjectId, Worktree | None]]:
        candidates: list[tuple[ProjectId, Worktree | None]] = []
        for project, registered in tuple(self._projects.items()):
            if registered.resolve() == root:
                candidates.append((project, None))
            if self._worktrees is not None:
                for worktree in await self._worktrees.live_for_project(project):
                    if worktree.path.resolve() == root:
                        candidates.append((project, worktree))
        return candidates

    async def resolve_legacy(
        self,
        project: ProjectId,
        destination: HandoffDestination,
    ) -> ResolvedRolloverDestination:
        """V1 continues in its validated source channel under the predecessor's project.

        Before destination metadata existed, a writer could report a different top level
        without registering it. That source is already bound by the pass's per-session root
        check. It never selects a new project, and it still needs a live branch and project.
        """
        if project not in self._projects:
            raise DestinationRefused("destination-unregistered")
        try:
            branch = await self._inspect(destination.root)
        except (OSError, RuntimeError) as error:
            raise DestinationRefused("destination-root") from error
        if branch is None:
            raise DestinationRefused("destination-root")
        if destination.branch and destination.branch != branch:
            raise DestinationRefused("branch-mismatch")
        return ResolvedRolloverDestination(project, replace(destination, branch=branch))
