"""Deterministic registered-first project catalogue policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol


class ProjectLike(Protocol):
    path: Path
    name: str
    area: str


@dataclass(frozen=True, slots=True)
class CatalogProject:
    opaque_id: str
    name: str
    area: str
    group: str


@dataclass(frozen=True, slots=True)
class CataloguePage:
    projects: tuple[CatalogProject, ...]
    page: int
    page_count: int
    degraded_reason: str | None


def build_catalogue(
    registered: Iterable[ProjectLike],
    discovered: Iterable[ProjectLike],
    *,
    registry_error: str | None = None,
) -> tuple[CatalogProject, ...]:
    """Merge canonical paths while preserving registry-first presentation."""
    used_paths: set[Path] = set()
    entries: list[CatalogProject] = []
    for project, group in ((item, "Registered") for item in registered):
        canonical = project.path.resolve(strict=False)
        if canonical not in used_paths:
            used_paths.add(canonical)
            entries.append(_entry(project, group, canonical))
    unregistered = []
    for project in discovered:
        canonical = project.path.resolve(strict=False)
        if canonical not in used_paths:
            used_paths.add(canonical)
            unregistered.append(_entry(project, "Unregistered", canonical))
    return tuple(
        entries
        + sorted(unregistered, key=lambda entry: (entry.area.casefold(), entry.name.casefold()))
    )


def search_catalogue(catalogue: Iterable[CatalogProject], query: str) -> tuple[CatalogProject, ...]:
    """Return stable case-insensitive name/area matches."""
    needle = query.casefold().strip()
    return tuple(
        project for project in catalogue if needle in f"{project.name} {project.area}".casefold()
    )


def paginate_catalogue(
    catalogue: Iterable[CatalogProject],
    page: int,
    page_size: int,
    *,
    registry_error: str | None = None,
) -> CataloguePage:
    """Return a deterministic one-indexed page without exposing filesystem paths."""
    projects = tuple(catalogue)
    if page_size < 1 or page < 1:
        raise ValueError("page and page_size must be positive")
    page_count = max(1, (len(projects) + page_size - 1) // page_size)
    if page > page_count:
        raise ValueError("page is out of range")
    start = (page - 1) * page_size
    return CataloguePage(projects[start : start + page_size], page, page_count, registry_error)


def _entry(project: ProjectLike, group: str, canonical: Path) -> CatalogProject:
    opaque_id = sha256(str(canonical).encode("utf-8")).hexdigest()[:24]
    return CatalogProject(opaque_id, project.name, project.area, group)
