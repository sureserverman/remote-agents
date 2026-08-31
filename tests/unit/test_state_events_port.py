"""The StateEvents push contract holds its shape before anything consumes it."""

from __future__ import annotations

import ast
from pathlib import Path

from remote_agents.ports.state_events import StateEvents

MODULE_PATH = Path("src/remote_agents/ports/state_events.py")


class _DuckSubscriber:
    def subscribe(self, listener: object) -> object:
        def unsubscribe() -> None:
            return None

        return unsubscribe


class _NotASubscriber:
    def publish(self, listener: object) -> None:
        return None


def test_a_duck_typed_object_with_subscribe_passes_isinstance() -> None:
    assert isinstance(_DuckSubscriber(), StateEvents)


def test_an_object_without_subscribe_fails_isinstance() -> None:
    assert not isinstance(_NotASubscriber(), StateEvents)


def test_internal_imports_touch_only_domain_and_ports() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    internal: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            internal.extend(
                alias.name for alias in node.names if alias.name.startswith("remote_agents")
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.module.startswith("remote_agents"):
                internal.append(node.module)
    for module in internal:
        assert module.startswith(("remote_agents.domain", "remote_agents.ports")), module
