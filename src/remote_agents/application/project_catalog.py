"""Deterministic registered-first project catalogue policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from remote_agents.ports.session_store import ProjectUsage

_SECONDS_PER_DAY = 86_400.0


class ProjectLike(Protocol):
    path: Path
    name: str
    area: str


class UsageReporting(Protocol):
    """The one thing the ranking wrapper asks of a session use case."""

    async def project_usage(self) -> Iterable[ProjectUsage]: ...


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


def rank_by_recent_use(
    catalogue: Iterable[CatalogProject],
    usage: Iterable[ProjectUsage],
    now: datetime,
    *,
    half_life_days: float = 14.0,
) -> tuple[CatalogProject, ...]:
    """Rank by launches whose weight halves every ``half_life_days``.

    The owner wants the projects they are working on *now* at the top, so a
    handful of launches yesterday must outrank a heavy burst from last year
    instead of a lifetime total deciding the order forever.

    ``now`` is an argument and nothing here reads a clock or a store: the same
    inputs must rank identically in a test, in a replay, and in a live request.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    scores = _usage_scores(usage, now, half_life_days)
    # sorted() is stable, so equal scores keep the registered-first, then
    # area/name order build_catalogue already established; ranking must never
    # invent a second tie-break of its own. Usage for a project missing from the
    # catalogue (deleted or unregistered) is simply never looked up.
    return tuple(sorted(catalogue, key=lambda project: -scores.get(project.opaque_id, 0.0)))


def last_used_by_project(usage: Iterable[ProjectUsage]) -> dict[str, datetime]:
    """When each project was last launched, keyed the way a catalogue row is (`opaque_id`).

    The local surface's projects pane shows a last-launch age beside every name, and it draws
    that from the same `project_usage` read that ranks the list -- so the age column and the
    order can never describe two different reads. Pure, like `rank_by_recent_use` beside it: a
    dict from the rows handed in, and nothing read.
    """
    return {str(entry.project_id): entry.last_used_at for entry in usage}


def order_alphabetically(catalogue: Iterable[CatalogProject]) -> tuple[CatalogProject, ...]:
    """Order by area then name, case-insensitively, and invent no second tie-break.

    The other half of the pair `rank_by_recent_use` opens: the owner who wants to *find* a
    project by name rather than resume the one they were just in. Registered-first is
    deliberately not preserved here — "alphabetical, except one group floats" is not
    alphabetical, and the owner asked for this order by name.

    sorted() is stable, so two entries whose casefolded (area, name) match keep the order
    they arrived in. Reaching for the opaque_id, the group or the path to separate them
    would be a second tie-break nobody asked for, and one whose result the owner cannot
    predict from what is on the screen.
    """
    return tuple(
        sorted(catalogue, key=lambda project: (project.area.casefold(), project.name.casefold()))
    )


async def rank_if_usage_is_reported(
    catalogue: tuple[CatalogProject, ...],
    sessions: UsageReporting | None,
    now: datetime,
    *,
    half_life_days: float = 14.0,
) -> tuple[CatalogProject, ...]:
    """Order the catalogue by recent use, or leave it exactly as it came.

    A host with no session use case cannot report usage, and the unranked catalogue is the
    honest answer rather than an empty one. This used to ask the launcher by name whether it
    could report usage at all, which gave a composition that forgot to wire one the same
    answer as a host that has none.

    It lives here rather than on either frontend because both surfaces now ask it (DEC-043):
    the rule that decides which order a project list opens in has one home, and each surface
    keeps its own sentence about it. `now` stays the caller's argument, so `rank_by_recent_use`
    remains pure and this wrapper reads no clock either.
    """
    if sessions is None:
        return catalogue
    usage = await sessions.project_usage()
    return rank_by_recent_use(catalogue, usage, now, half_life_days=half_life_days)


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


def _usage_scores(
    usage: Iterable[ProjectUsage],
    now: datetime,
    half_life_days: float,
) -> dict[str, float]:
    """Decay each project's session count by the age of its most recent session."""
    scores: dict[str, float] = {}
    for record in usage:
        elapsed = (now - record.last_used_at).total_seconds() / _SECONDS_PER_DAY
        # A clock-skewed future timestamp must not score above a genuine launch
        # made this second, so age floors at zero rather than amplifying.
        age_days = max(0.0, elapsed)
        scores[str(record.project_id)] = record.session_count * 0.5 ** (age_days / half_life_days)
    return scores


def _entry(project: ProjectLike, group: str, canonical: Path) -> CatalogProject:
    opaque_id = sha256(str(canonical).encode("utf-8")).hexdigest()[:24]
    return CatalogProject(opaque_id, project.name, project.area, group)
