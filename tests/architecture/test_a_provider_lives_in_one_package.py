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


# --- A provider's own setting keys, as values rather than as prose -------------------------
#
# A third sweep, added at the Stage 3 gate (2026-09-12) because the gate check written for this
# property could not express it. That check was
# `grep -rn "remoteControlAtStartup" src/remote_agents | cut -d: -f1 | sort -u` expecting one
# module; it returned six, five of which merely *explain* the key in a docstring -- the domain
# type saying what an unset value resolves to, the port saying why the value is not copied, the
# TUI screen saying what the row governs. The property was never violated and the check reported
# a defect that was not one.
#
# `test_the_console_key_budget_is_one_place._modules_with_an_argv_literal` met this same class
# first, for the tmux verbs a Stage 2 gate grepped for, and records the same conclusion: a string
# that is *code* has to be swept as code. This is the second instance, and the helper is written
# the same way deliberately so a reader meeting one finds the other.
#
# Why the property is worth a test at all, rather than only a corrected check: nothing else
# stops a surface from hard-coding `"remoteControlAtStartup"` and reading the file itself. A
# second reader of a provider's key would be free to disagree with the adapter about what an
# absent or wrong-typed value means -- which is the whole of what the adapter was careful about.

# **What actually separates prose from code here is exact equality, not the docstring skip.**
# `grep` matches a substring, so it found the key inside five sentences about it; this compares a
# constant's whole value, which no sentence containing the key can equal. The `docstrings` guard
# covers only the degenerate shape -- a module whose entire docstring *is* the key. Said plainly
# because the first version of the fixture below claimed to exercise that guard and did not: it
# passed with the guard deleted, which is the shape of a test that does not exist.

#: Each provider's own configuration keys, which its vertical may hold as values and nobody else
#: may. Not the provider's *identity* (that is `foreign_profile_literals` above) but the names it
#: uses inside its own files.
_PROVIDER_SETTING_KEYS = {
    "claude": ("remoteControlAtStartup",),
    "codex": ("remoteControlEnabled",),
}


def foreign_setting_key_literals(source_root: Path) -> list[str]:
    """Every provider setting key used as a value outside its own package, ignoring prose."""
    offenders = []
    modules = list(_package_modules(source_root))
    assert modules, "the sweep examined no modules"
    for relative, tree, _source in modules:
        owner = _owning_provider(relative)
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef | ast.Module)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or node in docstrings:
                continue
            for provider, keys in _PROVIDER_SETTING_KEYS.items():
                if node.value in keys and owner != provider:
                    offenders.append(f"{relative}: {node.value!r}")
    return offenders


def test_a_provider_setting_key_is_a_value_only_inside_its_own_package() -> None:
    """The spelling of another tool's config key is that vertical's knowledge (ARCH-02).

    A docstring naming the key is not a violation and must not be reported as one -- that is why
    this reads the AST and skips docstrings. What it catches is the thing that matters: a second
    module reading or writing the key itself, free to disagree with the adapter about what an
    absent or wrong-typed value means.
    """
    assert foreign_setting_key_literals(_SOURCE_ROOT) == []


def test_the_setting_key_sweep_finds_the_owner_so_it_is_not_vacuous() -> None:
    """Vacuity guard: the keys really are present as values in the packages that own them.

    Without this, deleting `REMOTE_CONTROL_AT_STARTUP_KEY` would leave the sweep above passing
    while the feature it guards no longer existed.
    """
    owners = {
        _owning_provider(relative)
        for relative, tree, _source in _package_modules(_SOURCE_ROOT)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and any(node.value in keys for keys in _PROVIDER_SETTING_KEYS.values())
    }
    assert owners == set(_PROVIDER_SETTING_KEYS), f"swept packages owning a key: {sorted(owners)}"


def test_a_foreign_setting_key_literal_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "src"
    module = root / "remote_agents" / "adapters" / "tui" / "smuggler.py"
    module.parent.mkdir(parents=True)
    module.write_text('KEY = "remoteControlAtStartup"\n', encoding="utf-8")

    assert foreign_setting_key_literals(root) == [
        "adapters/tui/smuggler.py: 'remoteControlAtStartup'"
    ]


def test_prose_naming_a_setting_key_is_not_reported(tmp_path: Path) -> None:
    """The false positive the corrected check exists to stop reporting -- both of its shapes.

    **Which guard does the work here is worth stating, because the obvious answer is wrong.** The
    first version of this test wrote a module whose docstring *mentioned* the key inside a sentence
    and asserted it was not reported -- and it passed with the `node in docstrings` guard deleted,
    so it was not testing what its name claimed. The reason is the real discriminator: `grep`
    matches a substring, while this sweep compares a constant's **whole value**, so a sentence
    containing the key is never equal to it and was never going to be reported.

    So the two shapes are separated here. The sentence is the case the Stage 3 gate check actually
    failed on, and exact equality is what acquits it. The bare docstring -- a module whose entire
    docstring *is* the key -- is the only shape the `docstrings` guard itself covers, and it is
    included so that deleting that guard turns this test red rather than leaving it passing for a
    reason unrelated to its name.
    """
    root = tmp_path / "src"
    sentence = root / "remote_agents" / "ports" / "explainer.py"
    sentence.parent.mkdir(parents=True)
    sentence.write_text(
        '"""Claude resolves remoteControlAtStartup at startup."""\n', encoding="utf-8"
    )
    bare = root / "remote_agents" / "ports" / "terse.py"
    bare.write_text('"""remoteControlAtStartup"""\n', encoding="utf-8")

    assert foreign_setting_key_literals(root) == []
