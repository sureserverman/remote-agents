"""Filesystem project workspace bounded by one configured development root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from remote_agents.adapters.projects.discovery import IGNORED_DIRECTORY_NAMES
from remote_agents.domain.projects import ProjectIdentity


@dataclass(frozen=True, slots=True)
class FilesystemProjectWorkspace:
    """Create and remove ``<dev_root>/<area>/<name>`` directories and nothing else."""

    dev_root: Path

    def areas(self) -> tuple[str, ...]:
        """List the existing area directories a new project may be created in."""
        try:
            entries = tuple(self.dev_root.iterdir())
        except OSError:
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in entries
                if entry.is_dir()
                and not entry.name.startswith(".")
                and entry.name not in IGNORED_DIRECTORY_NAMES
            )
        )

    def exists(self, identity: ProjectIdentity) -> bool:
        return self._project_path(identity).exists()

    def create(self, identity: ProjectIdentity) -> Path:
        """Create the project directory, refusing to replace anything already there."""
        path = self._project_path(identity)
        path.mkdir(parents=False, exist_ok=False)
        return path

    def remove(self, path: Path) -> None:
        """Remove an empty project directory this workspace could itself have created.

        Resolution decides only whether the path is in scope; the removal itself never
        follows a link, so a swapped-in symlink cannot redirect it at another project.
        """
        if path.is_symlink():
            raise ValueError("refusing to remove a symlinked project path")
        canonical = path.resolve(strict=True)
        if canonical.parent.parent != self.dev_root.resolve(strict=True):
            raise ValueError("refusing to remove a directory outside the development root")
        path.rmdir()

    def _project_path(self, identity: ProjectIdentity) -> Path:
        return self.dev_root / identity.area / identity.name
