"""Validated identity rules for a catalogued development project."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAXIMUM_LENGTH = 64


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    """One ``<area>/<name>`` pair that is safe as a directory and a registry token."""

    area: str
    name: str

    def __post_init__(self) -> None:
        for label, value in (("area", self.area), ("project name", self.name)):
            if not _SLUG.fullmatch(value) or len(value) > _MAXIMUM_LENGTH:
                raise ValueError(
                    f"{label} must be lowercase letters, digits, and single hyphens "
                    f"(1 to {_MAXIMUM_LENGTH} characters)"
                )

    def __str__(self) -> str:
        return f"{self.area}/{self.name}"
