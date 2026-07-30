"""Project browsing state that keeps catalogue paths out of Telegram callbacks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.application.project_catalog import CatalogProject

_GROUPS = ("Registered", "Unregistered")
_DEGRADED_MESSAGE = "The project catalogue is temporarily unavailable."


@dataclass(slots=True)
class CatalogueSnapshot:
    """Current safe catalogue projection supplied by the application composition root."""

    projects: tuple[CatalogProject, ...]
    registry_error: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectItem:
    """Display-only project data plus an opaque, server-resolved callback token."""

    name: str
    area: str
    callback_token: str


@dataclass(frozen=True, slots=True)
class ProjectSection:
    group: str
    items: tuple[ProjectItem, ...]


@dataclass(frozen=True, slots=True)
class ProjectView:
    group: str
    items: tuple[ProjectItem, ...]
    areas: tuple[str, ...]
    page: int
    page_count: int
    empty: bool
    degraded: bool
    reason: str | None


class ProjectNavigator:
    """Create stable project views and fail closed when a callback's project vanishes."""

    def __init__(
        self,
        catalogue: Callable[[], CatalogueSnapshot],
        callbacks: CallbackStateStore,
        *,
        page_size: int,
    ) -> None:
        if page_size < 1:
            raise ValueError("project page size must be positive")
        self._catalogue = catalogue
        self._callbacks = callbacks
        self._page_size = page_size

    def sections(
        self, *, owner_id: int, chat_id: int, view_revision: int
    ) -> tuple[ProjectSection, ...]:
        """Return Registered then Unregistered sections, even when a section is empty."""

        return tuple(
            ProjectSection(
                group,
                self.browse(
                    group=group,
                    page=0,
                    owner_id=owner_id,
                    chat_id=chat_id,
                    view_revision=view_revision,
                ).items,
            )
            for group in _GROUPS
        )

    def browse(
        self,
        *,
        group: str,
        page: int,
        owner_id: int,
        chat_id: int,
        view_revision: int,
        area: str | None = None,
        query: str | None = None,
    ) -> ProjectView:
        """Build a page with display metadata only; callbacks keep all meaning server-side."""

        if group not in _GROUPS:
            raise ValueError("project group is not supported")
        snapshot = self._catalogue()
        grouped = tuple(project for project in snapshot.projects if project.group == group)
        areas = tuple(sorted({project.area for project in grouped}, key=str.casefold))
        filtered = grouped
        if area is not None:
            filtered = tuple(project for project in filtered if project.area == area)
        if query is not None:
            needle = query.strip().casefold()
            filtered = tuple(
                project
                for project in filtered
                if needle in f"{project.name} {project.area}".casefold()
            )

        page_count = max(1, (len(filtered) + self._page_size - 1) // self._page_size)
        page_index = min(max(page, 0), page_count - 1)
        start = page_index * self._page_size
        visible = filtered[start : start + self._page_size]
        return ProjectView(
            group=group,
            items=tuple(
                ProjectItem(
                    name=project.name,
                    area=project.area,
                    callback_token=self._callbacks.create(
                        "project.select",
                        project.opaque_id,
                        owner_id,
                        chat_id,
                        view_revision,
                    ),
                )
                for project in visible
            ),
            areas=areas,
            page=page_index,
            page_count=page_count,
            empty=not filtered,
            degraded=snapshot.registry_error is not None,
            reason=_DEGRADED_MESSAGE if snapshot.registry_error is not None else None,
        )

    def resolve_selection(
        self, token: str, *, owner_id: int, chat_id: int, view_revision: int
    ) -> CatalogProject | None:
        """Re-read the catalogue so cached callbacks cannot launch a vanished project."""

        state = self._callbacks.resolve(
            token,
            owner_id=owner_id,
            chat_id=chat_id,
            view_revision=view_revision,
        )
        if state is None or state.action != "project.select":
            return None
        return next(
            (
                project
                for project in self._catalogue().projects
                if project.opaque_id == state.entity_id
            ),
            None,
        )

    def current_project(self, opaque_id: str) -> CatalogProject | None:
        """Resolve an opaque project ID against the current catalogue without a callback."""

        return next(
            (item for item in self._catalogue().projects if item.opaque_id == opaque_id), None
        )
