"""The production console's emitted set is complete, because what it does not emit it unbinds.

Since BL-096 the composer unbinds every key it tagged and no longer emits. So a production
construction that left a binding out would not merely miss a key: on its next entry it would
unbind that key from the owner's server. There is one production construction, and it passes the
root row plus the fold key. Both facts are pinned here, over the AST, so a second construction
or a shortened set fails before it ships.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "remote_agents"


def _constructions() -> list[tuple[str, ast.Call]]:
    found = []
    paths = sorted(_SOURCE.rglob("*.py"))
    assert paths, "the sweep examined nothing"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ConsoleComposer"
            ):
                found.append((str(path.relative_to(_SOURCE)), node))
    return found


def test_the_composition_root_is_the_only_production_console() -> None:
    assert [where for where, _ in _constructions()] == ["composition/tui.py"]


def test_the_production_console_emits_the_root_row_and_the_fold_key() -> None:
    ((_, call),) = _constructions()
    bindings = {keyword.arg: keyword.value for keyword in call.keywords}.get("bindings")
    assert bindings is not None, "the production console must name its bindings"
    assert ast.unparse(bindings) == "CONSOLE_BINDINGS + (console_panes_binding(),)"
