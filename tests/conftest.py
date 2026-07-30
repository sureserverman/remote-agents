"""Pytest controls for explicitly selected, side-effect-contained live profile checks."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("remote-agents")
    group.addoption(
        "--profile",
        action="append",
        default=[],
        metavar="PROFILE_ID",
        help="run the selected opt-in live profile qualification (repeatable)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live_profile: opt-in qualification using a generated dedicated tmux socket"
    )
