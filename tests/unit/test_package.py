"""Packaging-level checks that do not require any adapters."""

from __future__ import annotations

import tomllib
from pathlib import Path

from remote_agents import __version__

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_the_package_version_matches_the_one_the_project_is_built_with() -> None:
    """Compared against `pyproject.toml`, not against a literal repeated here.

    This test used to read `assert __version__ == "0.2.1"`, which is why the drift lasted:
    the two strings disagreed by three minor versions and the suite asserted the stale one,
    so the mirror was pinned rather than merely forgotten. A literal in a test is a third
    copy of the fact, and a third copy drifts exactly as the second one did.
    """
    declared = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert __version__ == declared, (
        f"the package reports {__version__} and pyproject.toml declares {declared}"
    )


def test_the_version_is_a_release_number_and_not_a_placeholder() -> None:
    parts = __version__.split(".")

    assert len(parts) == 3, __version__
    assert all(part.isdigit() for part in parts), __version__


def test_the_distribution_declares_the_metadata_a_registry_needs() -> None:
    """Classifiers and project URLs are present, so a built artifact is describable.

    Asserted here rather than left to `twine check`, which passes on a distribution carrying
    none of this: that command validates the metadata a project *declares*, and says nothing
    about metadata it omits. A gate that cannot fail on the absence it is meant to detect is
    not a gate, so the presence check lives where it can actually go red.

    The licence is deliberately not asserted. This project ships without one, which makes it
    all-rights-reserved by default -- a legal position its owner holds across every public
    repository they publish, and not one a test should quietly convert into a claim.
    """
    project = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]

    assert project.get("classifiers"), "no classifiers: the distribution is uncategorised"
    assert project.get("urls"), "no project URLs: a registry page would link nowhere"
    assert "Repository" in project["urls"], sorted(project["urls"])
