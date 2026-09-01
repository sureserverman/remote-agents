"""The surface list has one home, and it is derived from the frontend registry."""

from __future__ import annotations

import pytest
from surfaces import SURFACE_NAMES, surface_pairs


def test_the_names_come_from_the_frontend_registry() -> None:
    from remote_agents.adapters import telegram, tui

    assert SURFACE_NAMES == (telegram.FRONTEND.name, tui.FRONTEND.name)


def test_a_missing_surface_implementation_fails_loudly() -> None:
    """A frontend added to the registry must break every parity file until it is covered."""
    with pytest.raises(AssertionError, match="tui"):
        surface_pairs(telegram=object())


def test_pairs_come_back_in_registry_order_with_flattened_extras() -> None:
    pairs = surface_pairs(tui=("t", "extra"), telegram=("g",))
    assert pairs == (("telegram", "g"), ("tui", "t", "extra"))
