"""Catalogue ordering, opaque-ID, search, and pagination policy tests."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.application.project_catalog import (
    CatalogProject,
    build_catalogue,
    paginate_catalogue,
    rank_by_recent_use,
    search_catalogue,
)


@dataclass(frozen=True)
class Candidate:
    path: Path
    name: str
    area: str


@dataclass(frozen=True)
class Usage:
    """Stands in for the session store's per-project usage record."""

    project_id: str
    session_count: int
    last_used_at: datetime


def _identify(catalogue: tuple[CatalogProject, ...], name: str) -> str:
    return next(entry.opaque_id for entry in catalogue if entry.name == name)


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


def test_rank_puts_three_launches_yesterday_above_twenty_a_year_ago(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [],
        [
            Candidate(tmp_path / "infra" / "ancient", "ancient", "infra"),
            Candidate(tmp_path / "infra" / "recent", "recent", "infra"),
        ],
    )
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    usage = [
        Usage(_identify(catalogue, "ancient"), 20, now - timedelta(days=365)),
        Usage(_identify(catalogue, "recent"), 3, now - timedelta(days=1)),
    ]

    assert [entry.name for entry in catalogue] == ["ancient", "recent"]
    ranked = rank_by_recent_use(catalogue, usage, now, half_life_days=14)

    assert [entry.name for entry in ranked] == ["recent", "ancient"]


def test_rank_with_empty_usage_returns_the_catalogue_order_unchanged(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [Candidate(tmp_path / "infra" / "zulu", "zulu", "infra")],
        [
            Candidate(tmp_path / "web" / "vault", "vault", "web"),
            Candidate(tmp_path / "android" / "writer", "writer", "android"),
        ],
    )
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    assert rank_by_recent_use(catalogue, (), now, half_life_days=14) == catalogue
    assert rank_by_recent_use(catalogue, {}, now, half_life_days=14) == catalogue
    assert all(
        left is right
        for left, right in zip(
            rank_by_recent_use(catalogue, [], now, half_life_days=14), catalogue, strict=True
        )
    )


def test_rank_breaks_ties_on_registered_first_then_alphabetical_order(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [Candidate(tmp_path / "infra" / "zulu", "zulu", "infra")],
        [
            Candidate(tmp_path / "web" / "vault", "vault", "web"),
            Candidate(tmp_path / "android" / "writer", "writer", "android"),
        ],
    )
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    launched = now - timedelta(days=2)
    usage = {entry.opaque_id: Usage(entry.opaque_id, 5, launched) for entry in reversed(catalogue)}

    ranked = rank_by_recent_use(catalogue, usage, now, half_life_days=14)

    assert [entry.name for entry in ranked] == ["zulu", "writer", "vault"]
    assert [entry.name for entry in catalogue] == ["zulu", "writer", "vault"]


def test_rank_ignores_usage_for_a_project_missing_from_the_catalogue(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [],
        [
            Candidate(tmp_path / "infra" / "ancient", "ancient", "infra"),
            Candidate(tmp_path / "infra" / "recent", "recent", "infra"),
        ],
    )
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    usage = [
        Usage("0" * 24, 9_000, now),
        Usage(_identify(catalogue, "ancient"), 20, now - timedelta(days=365)),
        Usage(_identify(catalogue, "recent"), 3, now - timedelta(days=1)),
    ]

    ranked = rank_by_recent_use(catalogue, usage, now, half_life_days=14)

    assert [entry.name for entry in ranked] == ["recent", "ancient"]


def test_rank_reads_only_the_now_it_is_given_and_repeats_itself(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [],
        [
            Candidate(tmp_path / "infra" / "ancient", "ancient", "infra"),
            Candidate(tmp_path / "infra" / "recent", "recent", "infra"),
        ],
    )
    # Centuries from any wall clock: were a real clock consulted, both sessions
    # would read as future-dated, decay to nothing, and the raw counts would win.
    now = datetime(2400, 1, 1, tzinfo=UTC)
    usage = [
        Usage(_identify(catalogue, "ancient"), 20, now - timedelta(days=365)),
        Usage(_identify(catalogue, "recent"), 3, now - timedelta(days=1)),
    ]

    first = rank_by_recent_use(catalogue, usage, now, half_life_days=14)
    second = rank_by_recent_use(catalogue, usage, now, half_life_days=14)

    assert [entry.name for entry in first] == ["recent", "ancient"]
    assert first == second


def test_rank_rejects_a_non_positive_half_life(tmp_path: Path) -> None:
    catalogue = build_catalogue([], [Candidate(tmp_path / "infra" / "solo", "solo", "infra")])

    with pytest.raises(ValueError):
        rank_by_recent_use(catalogue, (), datetime(2026, 8, 10, tzinfo=UTC), half_life_days=0)
