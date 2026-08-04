"""Bounded, read-only discovery of projects beneath the configured development root."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

IGNORED_DIRECTORY_NAMES = ("archive", "archives")


@dataclass(frozen=True, slots=True)
class DiscoveredProject:
    """A canonical project directory discovered at exactly two levels below the root."""

    path: Path
    name: str
    area: str


def discover_projects(
    dev_root: Path, *, ignored_names: Iterable[str] = IGNORED_DIRECTORY_NAMES
) -> tuple[DiscoveredProject, ...]:
    """Discover safe ``<area>/<project>`` directories without traversing deeper."""
    ignored = frozenset(ignored_names)
    try:
        canonical_root = dev_root.resolve(strict=True)
    except OSError:
        return ()
    if not canonical_root.is_dir():
        return ()
    projects: list[DiscoveredProject] = []
    for area in _children(canonical_root):
        if not _eligible_name(area.name, ignored) or not _safe_directory(area, canonical_root):
            continue
        for candidate in _children(area):
            if not _eligible_name(candidate.name, ignored):
                continue
            canonical = _canonical_directory_within(candidate, canonical_root)
            if canonical is not None:
                projects.append(DiscoveredProject(canonical, candidate.name, area.name))
    return tuple(
        sorted(projects, key=lambda project: (project.area.casefold(), project.name.casefold()))
    )


def _children(path: Path) -> tuple[Path, ...]:
    try:
        return tuple(path.iterdir())
    except OSError:
        return ()


def _eligible_name(name: str, ignored: frozenset[str]) -> bool:
    return bool(name) and not name.startswith(".") and name not in ignored


def _safe_directory(path: Path, root: Path) -> bool:
    return _canonical_directory_within(path, root) is not None


def _canonical_directory_within(path: Path, root: Path) -> Path | None:
    try:
        canonical = path.resolve(strict=True)
    except OSError:
        return None
    if not canonical.is_dir() or not canonical.is_relative_to(root):
        return None
    return canonical
