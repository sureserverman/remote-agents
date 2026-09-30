"""The `live_network` gate: the owner's login leaves the host only when a run asks by name."""

from __future__ import annotations

import pytest
from live_network_gate import asked_for


@pytest.mark.parametrize(
    "markexpr",
    ["live_network", "live_network or slow", "(live_network)", "live_network and not slow"],
)
def test_an_expression_that_selects_by_the_marker_asks(markexpr: str) -> None:
    assert asked_for(markexpr) is True


@pytest.mark.parametrize(
    "markexpr",
    [
        "",
        None,
        "not live_network",
        "not requires_session",
        "not (slow and live_network)",
        "live_networking",
        "slow",
        "live_network and slow",
        "live_network or",
    ],
)
def test_an_expression_that_merely_does_not_exclude_it_does_not_ask(markexpr: str | None) -> None:
    assert asked_for(markexpr) is False
