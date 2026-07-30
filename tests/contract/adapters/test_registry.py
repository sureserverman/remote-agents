"""Contract tests for the read-only portfolio registry adapter."""

from hashlib import sha256
from pathlib import Path

import pytest

from remote_agents.adapters.projects.registry import load_registry
from remote_agents.application.project_catalog import paginate_catalogue


def fixture_path() -> Path:
    return Path(__file__).parents[2] / "fixtures" / "registry" / "valid.yaml"


def test_load_registry_uses_real_schema_and_only_enabled_entries() -> None:
    result = load_registry(fixture_path())

    assert result.error is None
    assert [(project.name, project.area) for project in result.projects] == [("project-a", "infra")]


@pytest.mark.parametrize(
    "body",
    [
        "version: 2\nprojects: []\n",
        "version: 1\nprojects: [not-a-map]\n",
        """version: 1
projects:
  - path: /tmp/a
    name: a
    area: infra
    enabled: true
""",
        """version: 1
projects:
  - path: relative/project
    name: a
    area: infra
    enabled: true
    added: x
""",
        """version: 1
projects:
  - path: /tmp/a
    name: a
    area: infra
    enabled: true
    added: x
    extra: true
""",
    ],
)
def test_load_registry_degrades_for_unknown_versions_or_malformed_entries(
    tmp_path: Path, body: str
) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(body, encoding="utf-8")

    assert load_registry(path).error is not None


def test_load_registry_rejects_duplicate_canonical_paths(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(
        "version: 1\nprojects:\n"
        "  - {path: /tmp/a/../project, name: one, area: infra, enabled: true, added: x}\n"
        "  - {path: /tmp/project, name: two, area: infra, enabled: true, added: x}\n",
        encoding="utf-8",
    )

    assert load_registry(path).error is not None


def test_load_registry_never_rewrites_fixture_bytes() -> None:
    path = fixture_path()
    before = sha256(path.read_bytes()).hexdigest()

    load_registry(path)

    assert sha256(path.read_bytes()).hexdigest() == before


def test_degraded_registry_error_is_safe_to_present_without_a_path(tmp_path: Path) -> None:
    missing_path = tmp_path / "private-registry.yaml"
    result = load_registry(missing_path)

    page = paginate_catalogue((), 1, 10, registry_error=result.error)

    assert result.error == "registry_unavailable"
    assert page.degraded_reason == "registry_unavailable"
    assert str(missing_path) not in page.degraded_reason
