"""Registered-first, callback-safe project browsing against a fake catalogue provider."""

from __future__ import annotations

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.projects import CatalogueSnapshot, ProjectNavigator
from remote_agents.application.project_catalog import CatalogProject


def project(opaque_id: str, name: str, area: str, group: str) -> CatalogProject:
    return CatalogProject(opaque_id, name, area, group)


def test_registered_projects_are_presented_before_unregistered_projects() -> None:
    navigator, _snapshot = navigator_for(
        (
            project("registered-1", "opaque-editor", "writing", "Registered"),
            project("unregistered-1", "opaque-verse", "writing", "Unregistered"),
        )
    )

    sections = navigator.sections(owner_id=7, chat_id=11, view_revision=1)

    assert [(section.group, [item.name for item in section.items]) for section in sections] == [
        ("Registered", ["opaque-editor"]),
        ("Unregistered", ["opaque-verse"]),
    ]


def test_group_area_search_and_pagination_keep_callbacks_opaque() -> None:
    navigator, _snapshot = navigator_for(
        (
            project("registered-1", "opaque-editor", "writing", "Registered"),
            project("registered-2", "writer-notes", "writing", "Registered"),
            project("registered-3", "opaque-ledger", "infra", "Registered"),
        ),
        page_size=1,
    )

    view = navigator.browse(
        group="Registered",
        area="writing",
        query="writer",
        page=1,
        owner_id=7,
        chat_id=11,
        view_revision=2,
    )

    assert view.page == 1
    assert view.page_count == 2
    assert [item.name for item in view.items] == ["writer-notes"]
    assert view.areas == ("infra", "writing")
    assert all(item.callback_token.startswith("c1_") for item in view.items)
    assert all("writer" not in item.callback_token for item in view.items)


def test_empty_and_degraded_catalogues_fail_closed_without_raw_reason() -> None:
    empty, _snapshot = navigator_for(())
    degraded, _snapshot = navigator_for((), registry_error="/home/user/private-registry")

    empty_view = empty.browse(group="Unregistered", page=0, owner_id=7, chat_id=11, view_revision=3)
    degraded_view = degraded.browse(
        group="Registered", page=0, owner_id=7, chat_id=11, view_revision=3
    )

    assert empty_view.empty is True
    assert empty_view.degraded is False
    assert degraded_view.empty is True
    assert degraded_view.degraded is True
    assert degraded_view.reason == "The project catalogue is temporarily unavailable."


def test_project_selection_re_resolves_opaque_id_and_rejects_a_vanished_project() -> None:
    navigator, snapshot = navigator_for(
        (project("registered-1", "opaque-editor", "writing", "Registered"),)
    )
    view = navigator.browse(group="Registered", page=0, owner_id=7, chat_id=11, view_revision=4)

    snapshot.projects = ()
    selected = navigator.resolve_selection(
        view.items[0].callback_token, owner_id=7, chat_id=11, view_revision=4
    )

    assert selected is None


def navigator_for(
    projects: tuple[CatalogProject, ...], *, page_size: int = 20, registry_error: str | None = None
) -> tuple[ProjectNavigator, CatalogueSnapshot]:
    snapshot = CatalogueSnapshot(projects, registry_error)
    return ProjectNavigator(lambda: snapshot, CallbackStateStore(), page_size=page_size), snapshot
