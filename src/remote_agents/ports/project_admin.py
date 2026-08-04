"""Technology-neutral contracts for creating and cataloguing one project."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from remote_agents.domain.projects import ProjectIdentity


class ProjectWorkspace(Protocol):
    """Filesystem boundary bounded by a single configured development root."""

    def areas(self) -> tuple[str, ...]: ...
    def exists(self, identity: ProjectIdentity) -> bool: ...
    def create(self, identity: ProjectIdentity) -> Path: ...
    def remove(self, path: Path) -> None: ...


class ProjectRegistry(Protocol):
    """Durable catalogue boundary that records exactly one created project."""

    def register(self, identity: ProjectIdentity, path: Path) -> None: ...
