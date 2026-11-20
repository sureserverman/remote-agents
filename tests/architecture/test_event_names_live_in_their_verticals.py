"""No provider's hook event name is a string constant in a shared module (BL-131, DEC-070).

What an event means is declared in its provider's licence, the hook writes that meaning into the
record as a neutral token, and the drain reads only the token. So a fifth provider declares its
events in its own package and edits no shared module. This keeps it that way.

**Derived from what the licences declare, not from a list of names**, as the dialog-wording rule
next door is: a hard-coded list would be a second copy of the thing this asserts is single-copy.

**An AST check, not a grep.** A grep reads prose, and the shared modules explain at length what
they no longer do. Docstrings are excluded; comments never reach the tree.

**A name that is also an ordinary word** -- Cursor's `stop`, Claude's `Stop` and `Notification`
-- is matched only where code decides by it: compared, tested for membership, looked up, used
as a key, or held in a set literal. Anywhere else it is a word, such as a label or a sentence,
and decides nothing.
"""

from __future__ import annotations

import ast
import pathlib

_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents"

#: The shared modules: the application and its ports and domain, and the spool, which is the one
#: adapter every provider's hook runs through.
_SHARED = (
    _SOURCE / "application",
    _SOURCE / "ports",
    _SOURCE / "domain",
    _SOURCE / "adapters" / "agents" / "activity_spool.py",
)


def _event_names() -> set[str]:
    """Every event name a provider's licence declares."""
    from remote_agents.adapters.agents.registry import provider_descriptors

    names: set[str] = set()
    for descriptor in provider_descriptors():
        licence = descriptor.activity
        if licence is None:
            continue
        names |= set(licence.events)
        names |= {
            name for name in (licence.started_event, licence.conversation_event) if name is not None
        }
    return names


def _is_ordinary_word(name: str) -> bool:
    """One alphabetic word in lower or title case: `stop`, `Stop`, `Notification`."""
    return name.isalpha() and (name.islower() or name[1:].islower())


def _docstrings(tree: ast.AST) -> set[ast.AST]:
    return {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef | ast.Module)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }


def _deciding_constants(tree: ast.AST) -> set[ast.Constant]:
    """String constants that code decides by: compared, tested, looked up or used as a key."""
    deciding: set[ast.Constant] = set()

    def strings(node: ast.AST) -> list[ast.Constant]:
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        ]

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for side in (node.left, *node.comparators):
                deciding.update(strings(side))
        elif isinstance(node, ast.Subscript):
            deciding.update(strings(node.slice))
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if key is not None:
                    deciding.update(strings(key))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"get", "pop", "setdefault", "__contains__"} and node.args:
                deciding.update(strings(node.args[0]))
        elif isinstance(node, ast.MatchValue):
            deciding.update(strings(node.value))
        elif isinstance(node, ast.Set):
            # A set literal exists to be tested against: `TURN_ENDED = frozenset({"Stop", ...})`.
            deciding.update(strings(node))
    return deciding


def offenders_in(source: str, names: set[str], where: str) -> list[str]:
    """Each event name `source` holds as a string constant where it counts, as `where: name`."""
    tree = ast.parse(source)
    docstrings = _docstrings(tree)
    deciding = _deciding_constants(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node in docstrings or node.value not in names:
            continue
        if _is_ordinary_word(node.value) and node not in deciding:
            continue
        found.append(f"{where}:{node.lineno}: {node.value!r}")
    return found


def _shared_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in _SHARED:
        files.extend([root] if root.is_file() else sorted(root.rglob("*.py")))
    return files


def test_no_shared_module_spells_a_providers_event_name() -> None:
    names = _event_names()
    assert {"Stop", "session.idle", "stop", "PermissionRequest"} <= names, (
        f"the licences declare {sorted(names)}; this check would pass vacuously"
    )

    offenders = []
    for path in _shared_files():
        offenders += offenders_in(
            path.read_text(encoding="utf-8"), names, str(path.relative_to(_SOURCE))
        )

    assert offenders == [], (
        "a provider's event name is a string constant in a shared module, so a fifth provider "
        "would have to edit shared code to declare its events (BL-131, DEC-070):\n"
        + "\n".join(offenders)
    )


def test_the_rule_sees_a_name_put_back_into_the_drain() -> None:
    """The rule's own red: the line BL-131 removed, put back, is found."""
    names = _event_names()

    assert offenders_in('if event == "session.idle":\n    pass\n', names, "x")
    assert offenders_in('kind = {"Stop": 1}.get(event)\n', names, "x")
    assert offenders_in('if event in ("stop", "other"):\n    pass\n', names, "x")
    assert offenders_in('ENDED = frozenset({"Stop"})\n', names, "x")


def test_an_ordinary_word_that_decides_nothing_is_not_an_event_name() -> None:
    names = _event_names()

    assert offenders_in('LABEL = "Stop"\n', names, "x") == []
    assert offenders_in('def f():\n    """Stop and session.idle, discussed."""\n', names, "x") == []
