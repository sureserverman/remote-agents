"""`remote-agents session-id` answers through the hook drain's rule, never a copy of it (DEC-128).

The verb exists so a handoff writer learns its session the way the activity hook does. A second
resolver beside `hook_identity.resolve` is the drift this guards against: the day one copy learns
a rule the other does not, a handoff and a hook disagree about whose session a Codex thread is.

Read from the AST of the module that implements the verb:

- the function the verb resolves through calls `resolve`, `inspect_roster` and `roster_path`,
  all imported from `adapters.agents.hook_identity`, and the hook's `read_roster` is built on
  that same `inspect_roster` -- one reader, so the verb and the hook trust the same files;
- the module reads no roster entry's placing fields and computes no thread time -- the shapes a
  second resolver would need. A tripwire, not a proof: a copy reaching them through `getattr` or
  other names passes it, and is the reviewer's to catch;
- the module spells no provider's thread variable: that is a descriptor fact (DEC-122);
- every licence that names its sessions from the payload declares its thread variable. Without
  one, its tool shells could only be named by the inherited variable DEC-128 forbids.
"""

from __future__ import annotations

import ast
from pathlib import Path

from remote_agents.adapters.agents.hook_licences import hook_licence
from remote_agents.agent_event import HOOK_PROVIDERS
from remote_agents.ports.provider_descriptor import HookIdentity, HookLicence

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_MODULE = _SOURCE / "agent_event.py"
_RESOLVER = "resolve_session_id"
_HOOK_IDENTITY = "remote_agents.adapters.agents.hook_identity"

#: What a resolver of its own would read off a roster entry, or compute from a thread id.
_RESOLVER_SHAPES = frozenset({"resume_source_id", "created_at", "workspace", "thread_created_at"})


def _tree() -> ast.Module:
    return ast.parse(_MODULE.read_text(encoding="utf-8"), filename=str(_MODULE))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name} in the parsed module")


def test_session_id_command_resolves_through_the_hook_identity_rule() -> None:
    function = _function(_tree(), _RESOLVER)
    imported = {
        alias.asname or alias.name
        for node in ast.walk(function)
        if isinstance(node, ast.ImportFrom) and node.module == _HOOK_IDENTITY
        for alias in node.names
    }
    called = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for needed in ("resolve", "inspect_roster", "roster_path"):
        assert needed in imported, f"{_RESOLVER} does not import {needed} from hook_identity"
        assert needed in called, f"{_RESOLVER} does not call {needed}"


def test_session_id_command_reads_the_roster_the_hook_reads() -> None:
    source = _SOURCE / "adapters" / "agents" / "hook_identity.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    called = {
        node.func.id
        for node in ast.walk(_function(tree, "read_roster"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "inspect_roster" in called, "read_roster no longer reads through inspect_roster"


def test_session_id_command_defines_no_resolver_of_its_own() -> None:
    found = sorted(
        f"{_MODULE.name}:{node.lineno}: {name}"
        for node in ast.walk(_tree())
        for name in (
            node.attr if isinstance(node, ast.Attribute) else None,
            node.id if isinstance(node, ast.Name) else None,
        )
        if name in _RESOLVER_SHAPES
    )
    assert not found, found


def _licences() -> dict[str, HookLicence]:
    found = {name: hook_licence(name) for name in HOOK_PROVIDERS}
    return {name: licence for name, licence in found.items() if licence is not None}


def test_session_id_command_spells_no_provider_thread_variable() -> None:
    variables = {
        licence.thread_variable
        for licence in _licences().values()
        if licence.thread_variable is not None
    }
    assert variables, "no licence declares a thread variable"
    found = sorted(
        f"{_MODULE.name}:{node.lineno}: {node.value!r}"
        for node in ast.walk(_tree())
        if isinstance(node, ast.Constant) and node.value in variables
    )
    assert not found, found


def test_session_id_command_has_a_thread_variable_for_every_payload_thread_licence() -> None:
    missing = sorted(
        name
        for name, licence in _licences().items()
        if licence.identity is HookIdentity.PAYLOAD_THREAD and not licence.thread_variable
    )
    assert not missing, missing
