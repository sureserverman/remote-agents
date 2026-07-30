"""Read-only adapter for the canonical portfolio projects registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RegisteredProject:
    """One enabled project from the trusted registry schema."""

    path: Path
    name: str
    area: str


@dataclass(frozen=True, slots=True)
class RegistryResult:
    """A successful catalogue or an explicitly degraded read failure."""

    projects: tuple[RegisteredProject, ...]
    error: str | None = None


def load_registry(path: Path) -> RegistryResult:
    """Parse version-one registry data without modifying its source bytes."""
    try:
        loaded = yaml.safe_load(path.read_bytes())
        return RegistryResult(projects=_parse_document(loaded))
    except (OSError, RegistrySchemaError, yaml.YAMLError) as error:
        return RegistryResult(projects=(), error=str(error))


class RegistrySchemaError(ValueError):
    """The registry did not conform to the supported version-one schema."""


def _parse_document(document: object) -> tuple[RegisteredProject, ...]:
    if not isinstance(document, dict) or set(document) != {"version", "projects"}:
        raise RegistrySchemaError("registry must contain only version and projects")
    if document["version"] != 1:
        raise RegistrySchemaError("unsupported registry version")
    entries = document["projects"]
    if not isinstance(entries, list):
        raise RegistrySchemaError("registry projects must be a list")
    projects: list[RegisteredProject] = []
    seen_paths: set[Path] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RegistrySchemaError(f"project {index} must be a mapping")
        required = {"path", "name", "area", "enabled", "added"}
        if set(entry) != required:
            raise RegistrySchemaError(f"project {index} has unknown or missing fields")
        if not isinstance(entry["enabled"], bool):
            raise RegistrySchemaError(f"project {index} enabled must be boolean")
        if not entry["enabled"]:
            continue
        if not all(isinstance(entry[key], str) and entry[key] for key in ("path", "name", "area")):
            raise RegistrySchemaError(f"project {index} has invalid text fields")
        configured_path = Path(entry["path"]).expanduser()
        if not configured_path.is_absolute():
            raise RegistrySchemaError(f"project {index} path must be absolute")
        canonical_path = configured_path.resolve(strict=False)
        if canonical_path in seen_paths:
            raise RegistrySchemaError("duplicate canonical project path")
        seen_paths.add(canonical_path)
        projects.append(RegisteredProject(canonical_path, entry["name"], entry["area"]))
    return tuple(projects)
