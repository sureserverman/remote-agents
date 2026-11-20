"""No live drill and no drill helper links an owner's login into a disposable home (BL-135).

Drills used to `os.symlink` the owner's `~/.codex/auth.json` (and OpenCode's) into a throwaway
home so the agent could take a real turn. On 2026-10-30 a token refresh replaced such a link
with a file, and the owner was probably logged out. Drills now bind the login read-only over an
empty placeholder inside `native_isolation`'s confinement. This rule keeps a link from coming
back: it parses every file under `tests/live` and `tests/support` and fails on a link call --
`os.symlink`, `os.link`, `Path.symlink_to`, `Path.hardlink_to` -- whose arguments or receiver name
`auth.json` or `.credentials.json` -- directly, through a variable assigned in the same module, or
through an `owner_*_login()` helper.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]
_SCANNED = (_TESTS / "live", _TESTS / "support")
_LOGINS = ("auth.json", ".credentials.json")
_LINK_FUNCTIONS = {"symlink", "link"}
_LINK_METHODS = {"symlink_to", "hardlink_to"}


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


def login_links(source: str) -> list[int]:
    """The lines of `source` that link a login file."""
    tree = ast.parse(source)
    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name):
                assigned.setdefault(target.id, []).append(node.value)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            name = function.attr
            receiver = [function.value] if name in _LINK_METHODS else []
        elif isinstance(function, ast.Name):
            name, receiver = function.id, []
        else:
            continue
        if name not in _LINK_FUNCTIONS | _LINK_METHODS:
            continue
        involved = [*node.args, *(keyword.value for keyword in node.keywords), *receiver]
        if any(_names_a_login(part, assigned, set()) for part in involved):
            found.append(node.lineno)
    return found


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
    }
    for source in flagged:
        assert login_links(source), source
    for source in (
        "os.symlink(here, there)",
        'assert not (codex_home / "auth.json").is_symlink()',
        'os.symlink(Path("/tmp/codex-daemon"), home / "app-server-control.sock")',
    ):
        assert login_links(source) == [], source
