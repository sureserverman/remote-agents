"""Provider identity is local: outside its package, a provider is named on an allowlist only.

Two sweeps, both structural (ARCH-02). The import sweep: no module outside a provider's own
package may import `remote_agents.adapters.agents.<provider>` except `registry.py`, the one
composer of the closed table (ARCH-04). The literal sweep: a `ProfileId("<provider>")`
constant outside the provider's package may appear only in the enumerated shared modules —
the curated tables, the trust vocabulary, and the surfaces' rendering seams (DEC-043: the
shared rule is asked there, not restated per provider). Growing either allowlist is a
deliberate, reviewable act.

Both sweeps carry the vacuity guard the sibling sweeps use: a root that yields no modules
raises rather than passing, because a gate that examined nothing is indistinguishable from
one that passed (DEC-010).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"

PROVIDERS = ("claude", "codex", "opencode", "cursor")

#: Modules outside a provider's package that may import it. One entry, by design.
IMPORT_ALLOWLIST = frozenset({"adapters/agents/registry.py"})

#: Modules outside a provider's package that may hold a `ProfileId("<provider>")` literal.
#: The current set, pinned exactly: the trust vocabulary, the lifecycle/action services that
#: dispatch on profile identity, and the two surface seams that render it.
PROFILE_LITERAL_ALLOWLIST = frozenset(
    {
        "domain/trust.py",
        "application/services.py",
        "application/session_actions.py",
        "adapters/telegram/service.py",
        "adapters/tmux/runtime.py",
    }
)

_PROFILE_IDS = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "cursor": "cursor-agent",
}
_LITERAL = re.compile(r'ProfileId\(\s*"(?P<value>[a-z-]+)"')


def _package_modules(source_root: Path) -> list[tuple[str, ast.Module, str]]:
    package = source_root / "remote_agents"
    modules = [
        (
            str(path.relative_to(package)),
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
            path.read_text(encoding="utf-8"),
        )
        for path in sorted(package.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    if not modules:
        raise AssertionError(f"the sweep found no modules under {package}; refusing to pass")
    return modules


def _owning_provider(relative: str) -> str | None:
    parts = Path(relative).parts
    nested = len(parts) >= 3 and parts[0] == "adapters" and parts[1] == "agents"
    if nested and parts[2] in PROVIDERS:
        return parts[2]
    return None


def foreign_vertical_imports(source_root: Path) -> list[str]:
    """Every import of a provider package from outside it, minus the allowlist."""
    offenders = []
    for relative, tree, _source in _package_modules(source_root):
        owner = _owning_provider(relative)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
                if node.module == "remote_agents.adapters.agents":
                    names += [f"remote_agents.adapters.agents.{alias.name}" for alias in node.names]
            for name in names:
                for provider in PROVIDERS:
                    prefix = f"remote_agents.adapters.agents.{provider}"
                    if name == prefix or name.startswith(prefix + "."):
                        if owner != provider and relative not in IMPORT_ALLOWLIST:
                            offenders.append(f"{relative}: imports {name}")
    return offenders


def foreign_profile_literals(source_root: Path) -> list[str]:
    """Every `ProfileId("<provider>")` literal outside its package, minus the allowlist."""
    offenders = []
    by_value = {value: provider for provider, value in _PROFILE_IDS.items()}
    for relative, _tree, source in _package_modules(source_root):
        owner = _owning_provider(relative)
        for match in _LITERAL.finditer(source):
            provider = by_value.get(match.group("value"))
            if provider is None:
                continue
            if owner != provider and relative not in PROFILE_LITERAL_ALLOWLIST:
                offenders.append(f'{relative}: ProfileId("{match.group("value")}")')
    return offenders


def test_no_module_outside_a_vertical_imports_it_except_the_registry() -> None:
    assert foreign_vertical_imports(_SOURCE_ROOT) == []


def test_provider_id_literals_stay_inside_the_allowlist() -> None:
    assert foreign_profile_literals(_SOURCE_ROOT) == []


def test_the_registry_actually_imports_every_vertical() -> None:
    """Vacuity guard for the import sweep: the one allowed importer really imports all four."""
    registry = (_SOURCE_ROOT / "remote_agents" / "adapters" / "agents" / "registry.py").read_text(
        encoding="utf-8"
    )
    for provider in PROVIDERS:
        assert "agents import" in registry or f"agents.{provider}" in registry, provider
    tree = ast.parse(registry)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "remote_agents.adapters.agents":
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            for provider in PROVIDERS:
                if node.module.startswith(f"remote_agents.adapters.agents.{provider}"):
                    imported.add(provider)
    assert set(PROVIDERS) <= imported, f"registry imports only {sorted(imported)}"


def test_a_foreign_vertical_import_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "src"
    module = root / "remote_agents" / "application" / "smuggler.py"
    module.parent.mkdir(parents=True)
    module.write_text("import remote_agents.adapters.agents.claude.usage\n", encoding="utf-8")

    offenders = foreign_vertical_imports(root)

    assert offenders == [
        "application/smuggler.py: imports remote_agents.adapters.agents.claude.usage"
    ]


def test_a_foreign_profile_literal_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "src"
    module = root / "remote_agents" / "application" / "smuggler.py"
    module.parent.mkdir(parents=True)
    module.write_text('WHO = ProfileId("cursor-agent")\n', encoding="utf-8")

    assert foreign_profile_literals(root) == ['application/smuggler.py: ProfileId("cursor-agent")']


def test_an_empty_tree_raises_rather_than_passing(tmp_path: Path) -> None:
    (tmp_path / "src" / "remote_agents").mkdir(parents=True)
    try:
        foreign_vertical_imports(tmp_path / "src")
    except AssertionError:
        return
    raise AssertionError("an empty module list passed the sweep")
