"""No key sequence in the terminal runtime escapes the check that it lands on the right screen.

`TmuxGateway.send_keys` types whatever it is given. Every sequence the runtime sends that ends in
`Enter` can approve a dialog it lands on -- every measured approval dialog opens on its yes
option (BL-055, DEC-063) -- so such a sequence goes through `send_keys_when`, which judges a
styled capture under the key lock first. What still calls `send_keys` directly is listed here,
each with the reason it may, and anything new fails this test until somebody writes its reason.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_RUNTIME = Path(__file__).resolve().parents[2] / "src/remote_agents/adapters/tmux/runtime.py"

#: (method, keys expression) -> how many such calls, and why each may send unguarded.
_ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("_remote_control", "REMOTE_CONTROL_DISMISS_MENU_KEYS"): (
        2,
        "`Escape` alone: it dismisses a menu and does nothing at a prompt or to a dialog's choice",
    ),
    ("_remote_control", "REMOTE_CONTROL_DISCONNECT_KEYS"): (
        1,
        "`Up Up Enter` only after the menu reads open on two consecutive captures a settle apart",
    ),
    ("answer_trust", "keys"): (
        1,
        "types into the folder-trust dialog by design, keys planned from its reading (DEC-079)",
    ),
    ("decline_trust", "keys"): (
        1,
        "declines the folder-trust dialog by design, gated on its classification (DEC-078)",
    ),
}


def keyed_calls(source: str) -> Counter[tuple[str, str]]:
    """Every `self._gateway.send_keys(...)` in `source`, by enclosing method and keys expression.

    Syntactic: a call through an alias (`gateway = self._gateway`) would not be found. The
    runtime has none; one added would need this sweep widened in the same change.
    """
    found: Counter[tuple[str, str]] = Counter()
    for method in ast.walk(ast.parse(source)):
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_keys"
                and ast.unparse(node.func.value) == "self._gateway"
            ):
                found[(method.name, ast.unparse(node.args[1]))] += 1
    return found


def test_every_keyed_call_in_the_runtime_is_listed_with_its_reason() -> None:
    found = keyed_calls(_RUNTIME.read_text(encoding="utf-8"))
    allowed = Counter({key: count for key, (count, _reason) in _ALLOWED.items()})

    unlisted = found - allowed
    assert not unlisted, f"unguarded key sequences with no stated reason: {dict(unlisted)}"
    stale = allowed - found
    assert not stale, f"listed reasons for calls that no longer exist: {dict(stale)}"


def test_the_keyed_sweep_catches_an_unlisted_call() -> None:
    """The sweep itself, proved against a mutant: a stop sent the old way is found."""
    source = _RUNTIME.read_text(encoding="utf-8")
    mutant = source + (
        "\n\nclass _Mutant:\n"
        "    async def graceful_stop(self, session_id, profile):\n"
        "        await self._gateway.send_keys(session_id, profile.graceful_keys)\n"
    )

    added = keyed_calls(mutant) - keyed_calls(source)

    assert added == Counter({("graceful_stop", "profile.graceful_keys"): 1})
