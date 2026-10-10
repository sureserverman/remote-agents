"""`remote_agents.adapters.git` is reached only through the composition package (DEC-015).

The git adapter is the one module that can create branches and delete folders in the owner's
repositories. `check_imports` already stops another adapter from importing it; this pins the
narrower rule that nothing outside `composition/` names it at all, and that the composition
really does build it, so the rule cannot pass on an adapter nobody wires.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_ADAPTER = "remote_agents.adapters.git"


def _importers(source: Path) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            if any(name == _ADAPTER or name.startswith(_ADAPTER + ".") for name in names):
                found.setdefault(relative, []).append(node.lineno)
    return found


def test_only_the_composition_package_imports_the_git_adapter() -> None:
    importers = _importers(_SOURCE)
    outside = {
        module: lines
        for module, lines in importers.items()
        if not module.startswith(("composition/", "adapters/git/"))
    }
    assert outside == {}, f"the git adapter is imported outside composition/: {outside}"
    assert any(module.startswith("composition/") for module in importers), (
        "nothing in composition/ builds the git adapter, so this rule is vacuous"
    )
