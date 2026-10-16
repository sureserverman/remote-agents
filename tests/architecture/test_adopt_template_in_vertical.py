"""A rollover's adopt message is spelled only in a provider's vertical (DEC-122, DEC-115).

What the rollover pass types into a successor is a provider fact: Claude Code runs the planning
plugin as `/planning:executing-plans`, Codex as `$planning:executing-plans`, and a `/` typed
into Codex is refused by its composer. So each vertical declares its own template on its
descriptor (`ProviderDescriptor.adopt_template`), and no shared module keeps one.

Read from the AST of every module outside `adapters/agents/`: no string constant there may
contain the adopt flag. Docstrings and bare string statements are skipped, so prose may still
name it; an f-string's literal parts are constants and are not skipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_VERTICALS = _SOURCE / "adapters" / "agents"
_FLAG = "--adopt-handoff"


def _prose(tree: ast.Module) -> set[int]:
    """The ids of string constants that stand alone as statements: docstrings and the like."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _spellings(path: Path, base: Path = _SOURCE) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    prose = _prose(tree)
    return [
        f"{path.relative_to(base)}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _FLAG in node.value
        and id(node) not in prose
    ]


def _shared_modules() -> list[Path]:
    return [path for path in sorted(_SOURCE.rglob("*.py")) if not path.is_relative_to(_VERTICALS)]


def test_no_shared_module_spells_an_adopt_template() -> None:
    modules = _shared_modules()
    assert len(modules) > 50, "the walk found almost no modules; it would pass reading nothing"
    found = [hit for path in modules for hit in _spellings(path)]
    assert found == [], (
        f"{_FLAG} is spelled outside a provider vertical at {found}; the adopt template is a "
        "descriptor fact (`ProviderDescriptor.adopt_template`, DEC-122)"
    )


def test_the_adopt_template_walk_catches_a_constant_and_spares_a_docstring(tmp_path: Path) -> None:
    """The guard's teeth, both directions: a template constant and an f-string part are found,
    a docstring and a comment are not."""
    module = tmp_path / "probe.py"
    module.write_text(
        '"""A docstring may say --adopt-handoff."""\n'
        "# so may a comment: --adopt-handoff\n"
        'TEMPLATE = "/x --adopt-handoff {handoff_id}"\n'
        'def f(i):\n    """Nor here: --adopt-handoff."""\n    return f"/x --adopt-handoff {i}"\n',
        encoding="utf-8",
    )
    assert _spellings(module, base=tmp_path) == ["probe.py:3", "probe.py:6"]


def test_the_adopt_templates_live_in_the_verticals_that_declare_them() -> None:
    """The positive half: the walk is not green because nobody spells a template anywhere."""
    from remote_agents.adapters.agents.registry import provider_descriptors

    declared = [d for d in provider_descriptors() if d.adopt_template is not None]
    assert declared, "no vertical declares an adopt template; nothing rolls over"
    for descriptor in declared:
        assert _FLAG in descriptor.adopt_template
