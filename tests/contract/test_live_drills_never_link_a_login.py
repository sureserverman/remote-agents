"""No live drill and no drill helper links an owner's login into a disposable home (BL-135).

Drills used to `os.symlink` the owner's `~/.codex/auth.json` (and OpenCode's) into a throwaway
home so the agent could take a real turn. On 2026-10-30 a token refresh replaced such a link
with a file, and the owner was probably logged out. Drills now bind the login read-only over an
empty placeholder inside `native_isolation`'s confinement. This rule keeps a link from coming
back. It parses every file under `tests/live` and `tests/support` and fails on any of these when
the call names `auth.json` or `.credentials.json` (directly, through a variable assigned in the
same module, or through an `owner_*_login()` helper):

- a link call: `os.symlink`, `os.link`, `Path.symlink_to`, `Path.hardlink_to`, under any alias;
- a call to a function of the module whose body makes a link call (a `_link(src, dst)` helper);
- an `ln -s` or `ln` argv handed to a subprocess or a pane;
- a writable bwrap `--bind` / `--bind-try` whose next element names a login.

What it cannot see is a name built at runtime (`"auth." + "json"`) or a link made by a program it
does not parse. The drills' own after-run checks of the owner's login are what catch those.

Claude drills that run in the owner's own HOME borrow no login and link nothing; this rule is
about logins handed to a disposable home, which for Claude is `native_isolation.claude_command`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]
_SCANNED = (_TESTS / "live", _TESTS / "support")
_LOGINS = ("auth.json", ".credentials.json")
_LINK_FUNCTIONS = {"symlink", "link"}
_LINK_METHODS = {"symlink_to", "hardlink_to"}
_WRITABLE_BINDS = {"--bind", "--bind-try"}
_LN = re.compile(r"(^|[\s;&|])ln(\s+-[a-zA-Z]+)*\s")


def _called(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else ""


def _names_a_login(node: ast.AST, assigned: dict[str, list[ast.AST]], seen: set[str]) -> bool:
    for part in ast.walk(node):
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            if any(login in part.value for login in _LOGINS):
                return True
        if isinstance(part, ast.Call) and _called(part).endswith("_login"):
            return True  # `native_isolation.owner_codex_login()` and its siblings
        if isinstance(part, ast.Name) and part.id in assigned and part.id not in seen:
            seen.add(part.id)
            if any(_names_a_login(value, assigned, seen) for value in assigned[part.id]):
                return True
    return False


def _assignments(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """Every value a name is given in the module, tuple unpacking and loop targets included."""
    assigned: dict[str, list[ast.AST]] = {}

    def bind(target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            assigned.setdefault(target.id, []).append(value)
        elif isinstance(target, ast.Tuple | ast.List):
            values = value.elts if isinstance(value, ast.Tuple | ast.List) else None
            if values is not None and len(values) == len(target.elts):
                for one, given in zip(target.elts, values, strict=True):
                    bind(one, given)
            else:
                for one in target.elts:
                    bind(one, value)
        elif isinstance(target, ast.Starred):
            bind(target.value, value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            bind(node.target, node.value)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            bind(node.target, node.iter)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            bind(node.optional_vars, node.context_expr)
    return assigned


def _link_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names that make a link: the primitives, their import aliases, and the module's own helpers
    whose bodies call one (to a fixed point, so a helper of a helper counts)."""
    functions, methods = set(_LINK_FUNCTIONS), set(_LINK_METHODS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _LINK_FUNCTIONS and alias.asname:
                    functions.add(alias.asname)
    definitions = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    grew = True
    while grew:
        grew = False
        for definition in definitions:
            if definition.name in functions:
                continue
            calls = (part for part in ast.walk(definition) if isinstance(part, ast.Call))
            if any(_called(call) in functions | methods for call in calls):
                functions.add(definition.name)
                grew = True
    return functions, methods


def _runs_ln(call: ast.Call) -> bool:
    for part in ast.walk(call):
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            if part.value == "ln" or _LN.search(part.value):
                return True
    return False


def _binds_writable(sequence: list[ast.AST], assigned, seen_factory) -> bool:
    for flag, following in zip(sequence, sequence[1:], strict=False):
        if (
            isinstance(flag, ast.Constant)
            and flag.value in _WRITABLE_BINDS
            and _names_a_login(following, assigned, seen_factory())
        ):
            return True
    return False


def login_links(source: str) -> list[int]:
    """The lines of `source` that link a login file, or bind one writable."""
    tree = ast.parse(source)
    assigned = _assignments(tree)
    functions, methods = _link_names(tree)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple) and _binds_writable(node.elts, assigned, set):
            found.add(node.lineno)
        if not isinstance(node, ast.Call):
            continue
        name = _called(node)
        receiver = (
            [node.func.value] if isinstance(node.func, ast.Attribute) and name in methods else []
        )
        involved = [*node.args, *(keyword.value for keyword in node.keywords), *receiver]
        names_login = any(_names_a_login(part, assigned, set()) for part in involved)
        if names_login and (name in functions or name in methods or _runs_ln(node)):
            found.add(node.lineno)
        if _binds_writable(list(node.args), assigned, set):
            found.add(node.lineno)
    return sorted(found)


def _sources() -> list[Path]:
    return sorted(path for root in _SCANNED for path in root.rglob("*.py"))


def test_no_drill_or_helper_links_a_login() -> None:
    sources = _sources()
    assert len(sources) > 20, f"the scan found only {len(sources)} files under {_SCANNED}"
    offenders = [
        f"{path.relative_to(_TESTS.parent)}:{line}"
        for path in sources
        for line in login_links(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "a drill links an owner's login; bind it read-only over a placeholder with "
        f"native_isolation.codex_command / opencode_command instead: {offenders}"
    )


def test_the_rule_sees_every_shape_a_link_took() -> None:
    """The five lines Task 3.2 and 3.3 removed, and the other ways of making the same link."""
    flagged = {
        'os.symlink(Path.home() / ".codex" / "auth.json", codex_home / "auth.json")',
        'os.symlink(source_auth, codex_home / "auth.json")',
        'source_auth = home / "auth.json"\nos.symlink(source_auth, target)',
        'owner = Path.home() / ".claude/.credentials.json"\nlink = owner\nos.link(link, there)',
        '(home / "auth.json").symlink_to(source)',
        'there.hardlink_to(Path.home() / ".claude" / ".credentials.json")',
        'symlink(src=owner_auth, dst="auth.json")',
        "os.symlink(owner_codex_login(), placeholder)",
        "os.symlink(native_isolation.owner_opencode_login(), target)",
        # The second review's shapes: a helper that takes the path, an alias, unpacking, `ln`,
        # and a writable bind.
        'def _link(src, dst):\n    os.symlink(src, dst)\n_link(home / "auth.json", there)',
        'def _a(s, d):\n    _b(s, d)\ndef _b(s, d):\n    s.symlink_to(d)\n_a(x, "auth.json")',
        'from os import symlink as ln\nln(owner, home / "auth.json")',
        'src, dst = Path.home() / ".codex" / "auth.json", there\nos.symlink(src, dst)',
        'subprocess.run(["ln", "-s", owner_codex_login(), home])',
        '_tmux("send-keys", f"ln -sf ~/.codex/auth.json {home}", "Enter")',
        'command = ["bwrap", "--bind", str(owner_codex_login()), str(target)]',
        'confined = ("bwrap", "--bind-try", "/home/u/.codex/auth.json", "/x/auth.json")',
    }
    for source in flagged:
        assert login_links(source), source
    for source in (
        "os.symlink(here, there)",
        'assert not (codex_home / "auth.json").is_symlink()',
        'os.symlink(Path("/tmp/codex-daemon"), home / "app-server-control.sock")',
        'command += ["--ro-bind", str(source), str(home / "auth.json")]',
        'command += ["--bind", str(directory), str(directory)]',
        'subprocess.run(["ln", "-s", "/tmp/a", "/tmp/b"])',
        'line = "kiln -s auth.json"',
    ):
        assert login_links(source) == [], source
