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
        # Failures are told through the bot, and a held notice is retried by the loop.
        assert composition.rollover_notifier is not None
        assert composition.rollover_notifier is composition.boundary.rollover_notifier
    finally:
        connection.close()
        ui.close()


def test_only_the_bot_composition_builds_or_runs_the_pass_anywhere_in_the_package() -> None:
    """Swept across every module, not only `composition/`: a pass built from `bootstrap`, an
    adapter or the TUI by any route -- the builder, the field, the loop -- would be a second
    process acting on panes the first one owns (DEC-030)."""
    allowed = {
        "build_rollover_pass": {"composition/service.py", "composition/telegram.py"},
        "rollover_pass=": {"composition/telegram.py"},
        "_roll_over_periodically": {"composition/service.py"},
        "RolloverPass(": {"composition/service.py"},
    }
    found: dict[str, set[str]] = {name: set() for name in allowed}
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in allowed:
            if name in text:
                found[name].add(str(path.relative_to(SRC)))
    found["RolloverPass("].discard("application/rollover.py")  # its own definition
    assert found["build_rollover_pass"], "the sweep found nothing to mean anything"
    for name, modules in found.items():
        assert modules <= allowed[name], (name, modules - allowed[name])
