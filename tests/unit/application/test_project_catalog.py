"""Catalogue ordering, opaque-ID, search, and pagination policy tests."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.application.project_catalog import (
    CatalogProject,
    build_catalogue,
    order_alphabetically,
    paginate_catalogue,
    rank_by_recent_use,
    rank_if_usage_is_reported,
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
    # Reversed on purpose: equal scores must fall back to the catalogue's own order, so the
    # order usage happens to arrive in must not influence the result.
    usage = [Usage(entry.opaque_id, 5, launched) for entry in reversed(catalogue)]

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


def test_the_two_opaque_id_derivations_cannot_drift_apart() -> None:
    """`bootstrap._opaque_id` and `project_catalog._entry` derive the same key, separately.

    Neither calls the other: one hashes `str(path.resolve(strict=False))`, the other hashes
    `str(canonical)` where the caller already resolved. They agree today, and nothing made
    them agree -- which is the BL-031 shape one level below the thing Stage 1 just fixed.

    What makes drift expensive rather than merely untidy is *how* it would fail. The opaque_id
    is the join key `with_project_names` looks a record up by. If the two derivations diverged,
    every lookup would miss, `with_project_name` would decline every record as "not in the
    catalogue" -- its documented, deliberate, silent fallback -- and both surfaces would go
    back to rendering the 24-character hash. No exception, no log, no failing test: the defect
    this stage exists to remove, restored by a change nobody would connect to it.

    Pinned rather than merged. Merging them means exporting a derivation from `application`
    and having the composition root call it, which is the better end state and is a change to
    the composition root -- not something to land at a stage gate. This test makes the drift
    impossible to land silently, which is the part that was actually missing.
    """
    from pathlib import Path

    from remote_agents.application.project_catalog import _entry
    from remote_agents.composition.backend import _opaque_id

    class _P:
        name = "demo"
        area = "infra"

    for path in (Path("/home/user/dev/infra/demo"), Path("/tmp/x"), Path("/")):
        canonical = path.resolve(strict=False)
        assert _entry(_P(), "Registered", canonical).opaque_id == _opaque_id(path), (
            f"the catalogue and the composition root disagree on the key for {path}"
        )


@dataclass
class UsageReporter:
    """Stands in for the session use case the composition root wires, or does not."""

    usage: list[Usage]
    calls: int = 0

    async def project_usage(self) -> list[Usage]:
        self.calls += 1
        return self.usage


def test_alphabetical_order_sorts_on_area_then_name_case_insensitively(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [Candidate(tmp_path / "Infra" / "zulu", "zulu", "Infra")],
        [
            Candidate(tmp_path / "web" / "Vault", "Vault", "web"),
            Candidate(tmp_path / "android" / "writer", "writer", "android"),
            Candidate(tmp_path / "infra" / "alpha", "alpha", "infra"),
        ],
    )

    ordered = order_alphabetically(catalogue)

    # Registered-first is deliberately *not* preserved: this is the order the owner asked
    # for by name, and "alphabetical, except one group floats" is not alphabetical.
    assert [entry.name for entry in ordered] == ["writer", "alpha", "zulu", "Vault"]


def test_alphabetical_order_invents_no_second_tie_break(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [],
        [
            Candidate(tmp_path / "one" / "infra" / "twin", "twin", "infra"),
            Candidate(tmp_path / "two" / "infra" / "TWIN", "TWIN", "infra"),
        ],
    )

    ordered = order_alphabetically(catalogue)

    # Same area, same casefolded name: a stable sort must leave them exactly as they came
    # rather than reaching for the opaque_id, the group, or the path to separate them.
    assert [entry.opaque_id for entry in ordered] == [entry.opaque_id for entry in catalogue]


async def test_ranking_leaves_the_catalogue_alone_when_the_host_reports_no_usage(
    tmp_path: Path,
) -> None:
    catalogue = build_catalogue(
        [Candidate(tmp_path / "infra" / "zulu", "zulu", "infra")],
        [Candidate(tmp_path / "web" / "vault", "vault", "web")],
    )
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    # A host with no session use case cannot report usage. The unranked catalogue is the
    # honest answer; an empty one is not.
    assert await rank_if_usage_is_reported(catalogue, None, now) == catalogue


async def test_ranking_asks_the_host_for_usage_and_applies_it(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [],
        [
            Candidate(tmp_path / "infra" / "ancient", "ancient", "infra"),
            Candidate(tmp_path / "infra" / "recent", "recent", "infra"),
        ],
    )
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    reporter = UsageReporter(
        [
            Usage(_identify(catalogue, "ancient"), 20, now - timedelta(days=365)),
            Usage(_identify(catalogue, "recent"), 3, now - timedelta(days=1)),
        ]
    )

    ranked = await rank_if_usage_is_reported(catalogue, reporter, now)

    assert [entry.name for entry in ranked] == ["recent", "ancient"]
    assert reporter.calls == 1


async def test_ranking_takes_now_from_its_caller_and_reads_no_clock(tmp_path: Path) -> None:
    catalogue = build_catalogue(
        [],
        [
            Candidate(tmp_path / "infra" / "ancient", "ancient", "infra"),
            Candidate(tmp_path / "infra" / "recent", "recent", "infra"),
        ],
    )
    # Centuries from any wall clock, for the reason
    # `test_rank_reads_only_the_now_it_is_given_and_repeats_itself` states: a real clock read
    # here would future-date both sessions, decay them to nothing, and let raw counts win.
    now = datetime(2400, 1, 1, tzinfo=UTC)
    reporter = UsageReporter(
        [
            Usage(_identify(catalogue, "ancient"), 20, now - timedelta(days=365)),
            Usage(_identify(catalogue, "recent"), 3, now - timedelta(days=1)),
        ]
    )

    ranked = await rank_if_usage_is_reported(catalogue, reporter, now)

    assert [entry.name for entry in ranked] == ["recent", "ancient"]
