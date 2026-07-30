"""Catalogue ordering, opaque-ID, search, and pagination policy tests."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from remote_agents.application.project_catalog import (
    build_catalogue,
    paginate_catalogue,
    search_catalogue,
)


@dataclass(frozen=True)
class Candidate:
    path: Path
    name: str
    area: str


def test_catalogue_deduplicates_paths_and_keeps_registered_entries_first(tmp_path: Path) -> None:
    shared = tmp_path / "infra" / "remote-agents"
    registered = [Candidate(shared, "remote-agents", "infra")]
    discovered = [
        Candidate(shared, "remote-agents", "infra"),
        Candidate(tmp_path / "web" / "vault", "vault", "web"),
    ]

    catalogue = build_catalogue(registered, discovered)

    assert [(entry.name, entry.group) for entry in catalogue] == [
        ("remote-agents", "Registered"),
        ("vault", "Unregistered"),
    ]


def test_catalogue_opaque_id_is_stable_and_does_not_expose_path(tmp_path: Path) -> None:
    path = tmp_path / "infra" / "remote-agents"

    first = build_catalogue([Candidate(path, "remote-agents", "infra")], [])
    second = build_catalogue([Candidate(path, "renamed", "infra")], [])

    assert first[0].opaque_id == second[0].opaque_id
    assert str(path) not in first[0].opaque_id


def test_catalogue_search_is_case_insensitive_and_deterministic(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [],
        [Candidate(tmp_path / "android" / "Opaque-Editor", "Opaque-Editor", "android")],
    )

    assert [entry.name for entry in search_catalogue(catalogue, "editor")] == ["Opaque-Editor"]
    assert [entry.name for entry in search_catalogue(catalogue, "ANDROID")] == ["Opaque-Editor"]


def test_catalogue_pagination_reports_degraded_registry_and_bounds(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [],
        [
            Candidate(tmp_path / "infra" / f"project-{number}", f"project-{number}", "infra")
            for number in range(3)
        ],
    )

    page = paginate_catalogue(catalogue, 2, 2, registry_error="bad registry")

    assert [entry.name for entry in page.projects] == ["project-2"]
    assert page.page_count == 2
    assert page.degraded_reason == "bad registry"
    with pytest.raises(ValueError):
        paginate_catalogue(catalogue, 3, 2)
