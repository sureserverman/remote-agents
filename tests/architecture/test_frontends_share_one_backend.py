"""Three boundaries the one-backend-two-frontends refactor built, pinned as rules.

Sub-plans 1 to 4 moved work out of two frontends and into one `Backend` plus a set of shared
use cases. Nothing about that arrangement is enforced by the type system: an adapter can
re-grow any of it at any time, and each regression would look like an ordinary commit.

**What each rule is, and what it is defending:**

1. **No adapter constructs a stop command.** Ending a session was written twice --
   `adapters/telegram/stops.py` and `adapters/tui/app.py` performed the same four steps
   against the same vocabulary and drifted anyway, for six days, on the one path that
   destroys a session (see `application/stops.py`). `application.stops` is the single
   dispatch; an adapter that builds a `GracefulStopCommand` itself has left it.
2. **No adapter discovers a backend capability by probing for it.** Absence is a declared
   field on `Backend`, checked as `is None`. It used to be a duck-typed probe --
   `getattr(self.launcher, "rename")` -- which sub-plan 1 removed in `75c86b6`, and which
   cannot distinguish "this host wired nothing" from "this object never had that method".
3. **A shared use case is defined once, under `application/`.** The names in
   `application/stops.py`, `session_actions.py` and `resume_flow.py` are the decisions both
   surfaces ask rather than restate (DEC-043). An adapter defining one of those names again
   is the re-duplication those sub-plans undid.

**These rules parse; they do not grep** (DEC-040, DEC-041). The distinction is not
stylistic. Sub-plan 3's gate found that the authored DEC-011 grep returned four hits and all
four were prose -- a substring match cannot tell a mention from a call. Sub-plan 2 paid the
same cost three times over, and DEC-043's corollary is the generalization: **guard the shape
a rule cannot be expressed without, never its name.** Rule 1 in particular resolves import
aliases, because sub-plan 3 recorded a name collision that forced an aliased import, and a
name-matching check walks straight past `import ForceStopCommand as Force`.

**Every set this file enumerates is pinned by its length** (DEC-041's practice on
`CONSOLE_BINDINGS`), so none can grow quietly. An equality assertion alone still passes when
the source set and the literal grow together, which is exactly what a careless edit does.

**Stated limits, because a check that overstates its coverage is worse than one that admits
its scope** (DEC-019):

- This is a **static, lexical** sweep over `src/remote_agents/adapters/`. It sees a call
  written in an adapter's own source, and nothing else.
- It cannot see a construction reached through a variable holding a class, through
  `globals()`, through an import performed at runtime, or from outside this package.
- Rule 2 catches a probe whose attribute name is a **string literal**. A probe built from a
  computed string is invisible to it, and deliberately so: the alternative is forbidding
  `getattr` outright, which this codebase legitimately uses on Textual objects.
- Rule 3 compares **names**, which is the one place a name is the right unit -- a
  redefinition under the same name is precisely the failure. It does not detect a shared use
  case copied into an adapter under a *different* name; nothing static can.

What these prove is that no *lexically visible* path re-grows the duplication. That is what
they are worth, and no more.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_ADAPTERS = _SOURCE / "adapters"
_APPLICATION = _SOURCE / "application"

#: The modules whose public names both surfaces ask rather than restate.
_SHARED_USE_CASE_MODULES = ("stops.py", "session_actions.py", "resume_flow.py")

#: Commands that destroy or end a session. Named here rather than derived, because the rule
#: is about *these* — the destructive ones — not about every command in the vocabulary.
_STOP_COMMANDS = frozenset({"GracefulStopCommand", "ForceStopCommand", "CleanupCommand"})

#: Where they legitimately live, and the one module allowed to build them.
_COMMANDS_MODULE = "remote_agents.application.commands"


def _adapter_modules() -> list[tuple[str, ast.Module]]:
    """Every adapter module, parsed, sorted by path so failures name a stable first offender."""
    return sorted(
        (
            (path.relative_to(_SOURCE).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
            for path in _ADAPTERS.rglob("*.py")
        ),
        key=lambda pair: pair[0],
    )


def _local_names_for(tree: ast.Module, imported: frozenset[str], module: str) -> set[str]:
    """The names `imported` are bound to *in this module*, following `as` aliases.

    A name-matching check reads the import list and stops. This follows the binding, because
    sub-plan 3 recorded a name collision that forced an aliased import — and against
    `from remote_agents.application.commands import ForceStopCommand as Force`, a check
    looking for `ForceStopCommand(` sees nothing at all while the call is right there.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            for alias in node.names:
                if alias.name in imported:
                    bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module and alias.asname is None:
                    # `import remote_agents.application.commands` — calls then read
                    # `remote_agents.application.commands.ForceStopCommand(...)`, which
                    # `_called_names` catches by its trailing attribute.
                    bound |= imported
    return bound


def _called_names(tree: ast.Module) -> set[str]:
    """Every name this module calls, by bare name or by trailing attribute.

    Over-approximates on purpose: the question is whether a construction is written here at
    all, and over-approximating is the safe direction for a check whose failure mode is
    missing one.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _literal_probes(tree: ast.Module) -> set[tuple[str, int]]:
    """Every `getattr`/`hasattr` on a string-literal attribute name, with its line."""
    probes: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            probes.add((node.args[1].value, node.lineno))
    return probes


def _defined_names(tree: ast.Module) -> set[str]:
    """Every function and class this module defines, at any nesting depth."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def _shared_use_case_names() -> set[str]:
    """The public names defined by the shared use-case modules, read rather than restated."""
    names: set[str] = set()
    for filename in _SHARED_USE_CASE_MODULES:
        tree = ast.parse((_APPLICATION / filename).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ) and not node.name.startswith("_"):
                names.add(node.name)
    return names


def _backend_fields() -> tuple[str, ...]:
    """`Backend`'s declared capabilities, read off the dataclass rather than restated."""
    from remote_agents.application.backend import Backend

    return tuple(Backend.__dataclass_fields__)


# --- Rule 1: no adapter constructs a stop command ---------------------------------------


def test_the_stop_command_set_is_the_three_destructive_ones() -> None:
    """Pinned by length as well as by content, so a fourth cannot join unnoticed."""
    assert len(_STOP_COMMANDS) == 3
    assert _STOP_COMMANDS == {"GracefulStopCommand", "ForceStopCommand", "CleanupCommand"}


def test_every_named_stop_command_exists() -> None:
    """The rule is worthless if it guards a name nothing defines — a renamed command would
    otherwise leave this file passing over a set of three ghosts."""
    from remote_agents.application import commands

    for name in _STOP_COMMANDS:
        assert hasattr(commands, name), f"{name} is guarded here but not defined in commands.py"


def test_no_adapter_constructs_a_stop_command() -> None:
    offenders = []
    for module, tree in _adapter_modules():
        bound = _local_names_for(tree, _STOP_COMMANDS, _COMMANDS_MODULE)
        built = bound & _called_names(tree)
        offenders.extend(f"{module}: builds {name}" for name in sorted(built))

    assert not offenders, (
        "a stop command is constructed inside an adapter; ending a session is "
        "`application.stops`'s one dispatch, and a second one drifted from the first for six "
        "days last time:\n  " + "\n  ".join(offenders)
    )


# --- Rule 2: no adapter probes for a backend capability ----------------------------------


def test_the_backend_capability_set_is_read_from_the_dataclass() -> None:
    """Read, not restated — and pinned by length so a tenth field is a decision, not a drift."""
    fields = _backend_fields()
    assert len(fields) == 9, (
        f"`Backend` now declares {len(fields)} fields, not 9. That is fine — but it widens "
        "what Rule 2 forbids probing for, so confirm the new field is a capability an adapter "
        "should read as a declared field rather than discover."
    )
    assert "sessions" in fields and "profiles" in fields


def test_no_adapter_discovers_a_backend_capability_by_probing() -> None:
    capabilities = set(_backend_fields())
    offenders = []
    for module, tree in _adapter_modules():
        for attribute, line in sorted(_literal_probes(tree)):
            if attribute in capabilities:
                offenders.append(f"{module}:{line}: probes for {attribute!r}")

    assert not offenders, (
        "a backend capability is discovered by probing rather than read as a declared field. "
        "Absence is a field that is None; a probe cannot tell a host that wired nothing from "
        "an object that never had the method (sub-plan 1 removed the last one in 75c86b6):\n  "
        + "\n  ".join(offenders)
    )


# --- Rule 3: a shared use case is defined once, under application/ -----------------------


def test_the_shared_use_case_set_is_read_from_its_modules() -> None:
    names = _shared_use_case_names()
    assert len(names) == 18, (
        f"the shared use-case modules now define {len(names)} public names, not 18. Adding one "
        "is ordinary; this assertion exists so that adding one is *noticed*, because every "
        "name here is a name no adapter may define."
    )
    assert {"resolve_stop", "dispatch_stop", "execute_stop"} <= names


@pytest.mark.parametrize("filename", _SHARED_USE_CASE_MODULES)
def test_each_shared_use_case_module_contributes_names(filename: str) -> None:
    """Per-module, so a module emptied or renamed cannot hide inside the total above."""
    tree = ast.parse((_APPLICATION / filename).read_text(encoding="utf-8"))
    public = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and not node.name.startswith("_")
    }
    assert public, f"{filename} defines no public use case; the set above is now short one module"


def test_no_adapter_redefines_a_shared_use_case() -> None:
    shared = _shared_use_case_names()
    offenders = []
    for module, tree in _adapter_modules():
        for name in sorted(_defined_names(tree) & shared):
            offenders.append(f"{module}: defines {name}")

    assert not offenders, (
        "a shared use case is defined again inside an adapter. The decision is shared and the "
        "sentence stays the surface's (DEC-043) — a surface may word the outcome its own way, "
        "but it asks rather than restating the rule:\n  " + "\n  ".join(offenders)
    )
