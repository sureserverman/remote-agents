"""Packaging-level checks that do not require any adapters."""

from remote_agents import __version__


def test_package_exposes_a_version() -> None:
    assert __version__ == "0.2.1"
