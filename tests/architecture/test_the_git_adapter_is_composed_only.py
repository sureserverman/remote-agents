"""`remote_agents.adapters.git` is reached only through the composition package (DEC-015).

The git adapter is the one module that can create branches and delete folders in the owner's
repositories. `check_imports` already stops another adapter from importing it; this pins the
narrower rule that nothing outside `composition/` names it at all, and that the composition
really does build it, so the rule cannot pass on an adapter nobody wires.
"""

from __future__ import annotations

import ast
from pathlib import Path

from check_imports import internal_imports

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_ADAPTER = "remote_agents.adapters.git"


def _importers(source: Path) -> dict[str, list[int]]:
    """Every module naming the adapter, relative imports resolved (`check_imports` does it)."""
    found: dict[str, list[int]] = {}
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source).as_posix()
        names = list(internal_imports(path, source.parent))
        # `from remote_agents.adapters import git` names the package by its alias, which
        # `internal_imports` folds into its parent module.
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names += [(node.lineno, f"{node.module}.{alias.name}") for alias in node.names]
        for line, imported in names:
            if imported == _ADAPTER or imported.startswith(_ADAPTER + "."):
                found.setdefault(relative, []).append(line)
    return {module: sorted(set(lines)) for module, lines in found.items()}


def test_a_relative_import_of_the_adapter_is_seen(tmp_path: Path) -> None:
    package = tmp_path / "remote_agents"
    (package / "application").mkdir(parents=True)
    (package / "application" / "sneaky.py").write_text(
        "from ..adapters.git import checkout\n", encoding="utf-8"
    )
    (package / "application" / "aliased.py").write_text(
        "from remote_agents.adapters import git\n", encoding="utf-8"
    )
    assert _importers(package) == {"application/sneaky.py": [1], "application/aliased.py": [1]}


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
