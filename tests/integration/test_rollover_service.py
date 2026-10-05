"""The rollover pass is the bot's service alone (DEC-115, DEC-030): `serve` builds and runs it,
and the local surface's composition names neither the pass nor its loop, so two processes can
never both launch, type or stop for one rollover."""

from __future__ import annotations

import ast
from pathlib import Path

from remote_agents.application.rollover import RolloverPass
from remote_agents.application.rollover_book import RolloverBook

SRC = Path(__file__).resolve().parents[2] / "src" / "remote_agents"


def test_the_local_surface_composes_no_rollover_pass() -> None:
    tree = ast.parse((SRC / "composition" / "tui.py").read_text(encoding="utf-8"))
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not (
        {"RolloverPass", "build_rollover_pass", "_roll_over_periodically"} & (names | imported)
    )


def test_the_bot_composition_wires_the_pass_and_both_surfaces_get_the_book(
    tmp_path, monkeypatch
) -> None:
    from test_composition_store_wiring import _composition

    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    try:
        assert isinstance(composition.rollover_pass, RolloverPass)
        assert isinstance(composition.boundary.backend.rollovers, RolloverBook)
    finally:
        connection.close()
        ui.close()
