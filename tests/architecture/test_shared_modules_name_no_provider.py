"""No module outside the four verticals compares a provider name or lists two of them.

BL-050 grew in a module this project's sweeps already covered: the activity spool compared
`provider == "opencode"`, `"cursor"` and `"codex"` and kept `MARKED_PROVIDERS`, and nothing
failed, because the provider sweep (`test_a_provider_lives_in_one_package.py`) read only
`ProfileId(...)` literals. A provider fact a shared module needs is declared in the vertical and
reached through the registry, `hook_licences` or composition (DEC-070); a shared module that
branches on a name is a second copy of that fact, and the one a fifth provider would miss.

The shapes, read from the AST so prose that *discusses* a provider is never a finding. A
"name" is a string constant that is a hook name or a profile id, or `ProfileId("<one>")`:

- a comparison with a name (`provider == "codex"`, `profile_id != ProfileId("claude")`);
- a comparison whose right side is a collection holding any name (`p in ("codex",)`);
- a set, tuple or list literal holding two or more names -- `frozenset({...})` included, since
  its argument is one;
- a `case` pattern matching a name.

Not caught, and said so rather than implied: a name bound to a module constant and compared
through it, and a method call taking a name (`.startswith("codex")`). A sweep of `src` on
2026-10-10 found neither shape; a reader adding one is the reviewer's to catch.

Not a lookup keyed by name. `adapters/tmux/profiles.py` and `domain/profiles.py` key their
curated tables by profile, and DEC-070 kept those tables where they are; this rule is about
deciding behaviour by comparing a name, not about naming a row.

The names come from the registry rather than a word list, so a fifth provider is covered the day
its descriptor lands.
"""

from __future__ import annotations

import ast
import pathlib

_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_VERTICALS = ("claude", "codex", "opencode", "cursor")

#: Module, assigned name: the one place a list of provider names is the point. `HOOK_PROVIDERS`
#: is the `--provider` choices, spelled in `agent_event.py` so the hook stays importable without
#: the adapters; `tests/integration/test_agent_hook_install.py` holds it equal to the
#: descriptors' hook names.
_ALLOWED = frozenset({("agent_event.py", "HOOK_PROVIDERS")})

#: Module, function: the four places that ask "is this claude?" for Remote Control, which is a
#: pane action only Claude has and which DEC-070 did not move into the vertical. The same four
#: modules are on `test_a_provider_lives_in_one_package.py`'s `PROFILE_LITERAL_ALLOWLIST` for
#: the same reason. Recorded here so the next comparison is a finding, not a fifth site; moving
#: Remote Control's provider fact into the claude vertical would empty this set.
_ALLOWED_FUNCTIONS = frozenset(
    {
        ("application/session_actions.py", "remote_control_available"),
        ("application/services.py", "set_remote_control"),
        ("adapters/tmux/runtime.py", "_remote_control"),
        ("adapters/tmux/runtime.py", "remote_control_state"),
    }
)


def _provider_names() -> frozenset[str]:
    from remote_agents.adapters.agents.registry import provider_descriptors

    names: set[str] = set()
    for descriptor in provider_descriptors():
        names.add(str(descriptor.profile_id))
        if isinstance(descriptor.hooks, str):
            names.add(descriptor.hooks)
    return frozenset(names)


def _is_vertical(relative: pathlib.Path) -> bool:
    parts = relative.parts
    return len(parts) > 3 and parts[:2] == ("adapters", "agents") and parts[2] in _VERTICALS


def _allowed_nodes(relative: str, tree: ast.Module) -> set[int]:
    """The ids of every node inside an allow-listed assignment."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (relative, node.name) in _ALLOWED_FUNCTIONS:
                allowed |= {id(inner) for inner in ast.walk(node)}
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and (relative, target.id) in _ALLOWED:
                    allowed |= {id(inner) for inner in ast.walk(node)}
    return allowed


def provider_name_branches(source: pathlib.Path, names: frozenset[str]) -> list[str]:
    """Every comparison with, or collection of, provider names outside the verticals."""

    def named(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
            return node.value
        call = isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        if call and node.func.id == "ProfileId" and len(node.args) == 1:
            return named(node.args[0])
        return None

    def collected(node: ast.AST) -> list[str]:
        if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            return [hit for hit in map(named, node.elts) if hit]
        return []

    modules = [
        path
        for path in sorted(source.rglob("*.py"))
        if "__pycache__" not in path.parts and not _is_vertical(path.relative_to(source))
    ]
    if not modules:
        raise AssertionError(f"the sweep found no modules under {source}; refusing to pass")
    offenders = []
    for path in modules:
        relative = str(path.relative_to(source))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        allowed = _allowed_nodes(relative, tree)
        for node in ast.walk(tree):
            if id(node) in allowed:
                continue
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                hits = [hit for hit in map(named, operands) if hit]
                hits += [hit for operand in node.comparators for hit in collected(operand)]
                # Reported as the comparison; the collection itself is not a second finding.
                allowed |= {id(operand) for operand in node.comparators}
                if hits:
                    offenders.append(f"{relative}:{node.lineno}: compares {hits}")
            elif isinstance(node, ast.MatchValue) and named(node.value):
                offenders.append(f"{relative}:{node.lineno}: matches {[named(node.value)]}")
            elif len(collected(node)) >= 2:
                offenders.append(f"{relative}:{node.lineno}: lists {collected(node)}")
    return offenders


def test_no_shared_module_compares_or_lists_provider_names() -> None:
    assert provider_name_branches(_SOURCE, _provider_names()) == []


def test_the_names_cover_every_hook_name_and_profile_id() -> None:
    """Cursor's hook is `cursor` and its profile `cursor-agent`; both spellings are names."""
    names = _provider_names()
    assert {"claude", "codex", "opencode", "cursor", "cursor-agent"} <= names


def test_the_allowed_tuple_is_still_there_to_allow() -> None:
    """An allow-list entry whose assignment is gone allows nothing and should go with it."""
    tree = ast.parse((_SOURCE / "agent_event.py").read_text(encoding="utf-8"))
    assert _allowed_nodes("agent_event.py", tree), "HOOK_PROVIDERS is no longer assigned there"


def test_a_comparison_in_a_shared_module_is_reported(tmp_path: pathlib.Path) -> None:
    module = tmp_path / "remote_agents" / "application" / "branchy.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'def f(provider):\n    if provider == "codex":\n        pass\n'
        '    return provider in frozenset({"claude", "cursor-agent"})\n',
        encoding="utf-8",
    )

    offenders = provider_name_branches(tmp_path / "remote_agents", _provider_names())

    assert offenders == [
        "application/branchy.py:2: compares ['codex']",
        "application/branchy.py:4: lists ['claude', 'cursor-agent']",
    ]


def test_the_shapes_a_review_found_unseen_are_reported(tmp_path: pathlib.Path) -> None:
    """Found by the Stage 3 reviews, which ran the first version of this sweep against them."""
    module = tmp_path / "remote_agents" / "application" / "shapes.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "def f(p, record):\n"
        '    a = p in ["claude", "codex"]\n'
        '    b = p in ("codex",)\n'
        '    c = record.profile_id != ProfileId("claude")\n'
        "    match p:\n"
        '        case "opencode":\n'
        "            pass\n",
        encoding="utf-8",
    )

    offenders = provider_name_branches(tmp_path / "remote_agents", _provider_names())

    assert offenders == [
        "application/shapes.py:2: compares ['claude', 'codex']",
        "application/shapes.py:3: compares ['codex']",
        "application/shapes.py:4: compares ['claude']",
        "application/shapes.py:6: matches ['opencode']",
    ]


def test_every_allowed_function_still_exists_to_allow() -> None:
    """An allowance whose function is gone allows nothing and should go with it."""
    for relative, function in _ALLOWED_FUNCTIONS:
        tree = ast.parse((_SOURCE / relative).read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert function in names, f"{relative} no longer defines {function}"


def test_a_vertical_may_name_itself_and_prose_is_never_a_finding(tmp_path: pathlib.Path) -> None:
    vertical = tmp_path / "remote_agents" / "adapters" / "agents" / "codex" / "hooks.py"
    vertical.parent.mkdir(parents=True)
    vertical.write_text('X = "codex" == "codex"\n', encoding="utf-8")
    shared = tmp_path / "remote_agents" / "application" / "prose.py"
    shared.parent.mkdir(parents=True)
    shared.write_text('"""Unlike codex, claude never..."""\nNAME = "codex"\n', encoding="utf-8")

    assert provider_name_branches(tmp_path / "remote_agents", _provider_names()) == []
