"""No key sequence in the terminal runtime escapes the check that it lands on the right screen.

`TmuxGateway.send_keys` types whatever it is given. Every sequence the runtime sends that ends in
`Enter` can approve a dialog it lands on -- every measured approval dialog opens on its yes
option (BL-055; DEC-063's never-approve clause) -- so such a sequence goes through
`send_keys_when`, which judges a styled capture under the key lock first. What still calls
`send_keys` directly is listed here, each with the reason it may, and anything new fails this
test until somebody writes its reason.
The rule is DEC-103.
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
        "`Escape` alone approves nothing: it dismisses the menu our own `/remote-control` opened, "
        "and at worst declines a dialog or interrupts a turn",
    ),
    ("answer_trust", "keys"): (
        1,
        "types into the folder-trust dialog by design, keys planned from its reading (DEC-079); "
        "read outside the send's lock hold, so only a second trust answer can race it",
    ),
    ("decline_trust", "keys"): (
        1,
        "declines the folder-trust dialog by design, gated on its reading (DEC-078); read "
        "outside the send's lock hold, so only a second trust answer can race it",
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


def test_every_agent_whose_stop_submits_declares_a_composer() -> None:
    """A stop ending in `Enter` is judged by the agent's composer; without one it goes unchecked.

    `TmuxTerminal.graceful_stop` sends an unguarded stop for an agent with no composer, which is
    only safe while no such agent's stop submits anything. This is what keeps that true.
    """
    from remote_agents.adapters.agents.registry import profile_composers
    from remote_agents.domain.profiles import closed_profiles

    composers = profile_composers()
    submitting = [
        str(profile.profile_id) for profile in closed_profiles() if "Enter" in profile.graceful_keys
    ]
    assert submitting, "no stop submits anything, so this check would be vacuous"

    assert [agent for agent in submitting if agent not in composers] == []


#: (method, keys expression) -> how many such guarded calls, and what their check refuses.
_GUARDED: dict[tuple[str, str], tuple[int, str]] = {
    ("graceful_stop", "profile.graceful_keys"): (
        1,
        "a draft, shell mode, a dialog or the open Remote Control menu; a dialog between keys",
    ),
    ("_remote_control", "REMOTE_CONTROL_ENABLE_KEYS"): (
        1,
        "anything but an idle composer not already on; a dialog between keys",
    ),
    ("_remote_control", "REMOTE_CONTROL_OPEN_MENU_KEYS"): (
        1,
        "anything but an idle composer with no menu up and not already off; a dialog between keys",
    ),
    ("_remote_control", "REMOTE_CONTROL_DISCONNECT_KEYS"): (
        1,
        "a screen whose menu is not open, before the first arrow and each after it",
    ),
}


def guarded_calls(
    source: str,
) -> list[tuple[str, str, ast.expr, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every `self._gateway.send_keys_when(...)`: method, keys expression, check, and the method."""
    found = []
    for method in ast.walk(ast.parse(source)):
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_keys_when"
                and ast.unparse(node.func.value) == "self._gateway"
            ):
                found.append((method.name, ast.unparse(node.args[1]), node.args[2], method))
    return found


def _constant(check: ast.expr, method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A check that decides nothing: a lambda of a constant, or a local function returning one."""
    if isinstance(check, ast.Lambda):
        return isinstance(check.body, ast.Constant)
    if isinstance(check, ast.Name):
        for inner in ast.walk(method):
            if isinstance(inner, ast.FunctionDef) and inner.name == check.id:
                returns = [node for node in ast.walk(inner) if isinstance(node, ast.Return)]
                return all(isinstance(ret.value, ast.Constant) for ret in returns)
    return False


def test_every_guarded_send_is_listed_with_what_it_refuses() -> None:
    calls = guarded_calls(_RUNTIME.read_text(encoding="utf-8"))
    found = Counter((method, keys) for method, keys, _check, _where in calls)
    listed = Counter({key: count for key, (count, _reason) in _GUARDED.items()})

    assert not found - listed, f"guarded sends nobody has described: {dict(found - listed)}"
    assert not listed - found, (
        f"described guarded sends that no longer exist: {dict(listed - found)}"
    )


def test_no_guarded_send_is_licensed_by_a_constant_check() -> None:
    """A guarded send with `lambda capture: True` is an unguarded send the first sweep misses."""
    constant = [
        (method, keys)
        for method, keys, check, where in guarded_calls(_RUNTIME.read_text(encoding="utf-8"))
        if _constant(check, where)
    ]

    assert constant == []


def test_the_guarded_sweep_catches_a_constant_check() -> None:
    """The sweep, proved against a mutant: a stop licensed by `lambda capture: True`."""
    mutant = _RUNTIME.read_text(encoding="utf-8") + (
        "\n\nclass _Mutant:\n"
        "    async def graceful_stop(self, session_id, profile):\n"
        "        await self._gateway.send_keys_when(\n"
        "            session_id, profile.graceful_keys, lambda capture: True\n"
        "        )\n"
    )

    flagged = [
        (method, keys)
        for method, keys, check, where in guarded_calls(mutant)
        if _constant(check, where)
    ]
    assert flagged == [("graceful_stop", "profile.graceful_keys")]
