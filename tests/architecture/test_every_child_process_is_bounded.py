"""Every child process this project starts has a bound, so nothing it runs can hang it forever.

`upgrade` runs `onboard --install-daemon` with no bound of its own, on the promise that the child
bounds every command it runs (`composition/onboarding.py`, `_ONBOARD_SECONDS`). The close-out
evaluator found the promise false: `doctor`'s tmux feature probe ran its commands with no
timeout, so a stuck tmux would have hung `upgrade` with nothing to end it. This sweeps the whole
source for the class rather than the one instance.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "remote_agents"

_CALLS = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}

#: (file relative to the package, call line's function) -> why it may run unbounded.
_UNBOUNDED: dict[tuple[str, str], str] = {
    ("statusline.py", "hop_from_stdin"): (
        "runs the owner's own status-line command, passed through as it was configured; "
        "Claude Code, not this hop, owns how long a status line may take"
    ),
}


def _unbounded(source: str) -> list[tuple[str, int]]:
    found = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) in _CALLS
                and not any(keyword.arg == "timeout" for keyword in node.keywords)
            ):
                found.append((function.name, node.lineno))
    return found


def test_every_child_process_is_started_with_a_timeout() -> None:
    offenders = [
        f"{path.relative_to(_SOURCE)}:{line} in {function}"
        for path in sorted(_SOURCE.rglob("*.py"))
        for function, line in _unbounded(path.read_text(encoding="utf-8"))
        if (str(path.relative_to(_SOURCE)), function) not in _UNBOUNDED
    ]

    assert offenders == [], offenders


def test_every_listed_exception_still_exists() -> None:
    for (relative, function), _reason in _UNBOUNDED.items():
        source = (_SOURCE / relative).read_text(encoding="utf-8")
        assert function in {name for name, _line in _unbounded(source)}, (relative, function)


def test_the_sweep_finds_an_unbounded_call() -> None:
    mutant = "import subprocess\n\ndef probe():\n    subprocess.run(['tmux', 'ls'], check=True)\n"

    assert _unbounded(mutant) == [("probe", 4)]
