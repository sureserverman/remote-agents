"""The tmux verbs that destroy a managed session may not be built anywhere in the codec.

`join-pane` and `move-pane` relocate a pane into another window. Do that with a managed
session's only pane and its window is left empty, so tmux destroys the window — and the
session with it, taking the `@remote_agents_*` identity too. Probed on tmux 3.4 (2026-08-19)
and recorded as DEC-040's rejected alternative, which is the shape the whole swap design
exists to avoid: `swap-pane` exchanges two panes and leaves both windows occupied.

The plan that built this had a gate check spelling the same rule as a `grep`, and a gate check
runs on the day somebody runs the plan. It also matched *prose* — it first failed on a docstring
arguing why the mechanism is rejected, which is an argument worth keeping rather than deleting
to satisfy a pattern. So the rule lives here instead, as the codec's own argv vocabulary: what
is forbidden is *building the command*, not writing its name.

`break-pane` is in the set for the same reason from the other direction — it moves a pane out
into a window of its own, which is how a console pane would silently stop being the console's.
"""

from __future__ import annotations

import ast
import pathlib

_CODEC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "remote_agents"
    / "adapters"
    / "tmux"
    / "codec.py"
)

#: Verbs that can empty a window, and so destroy the session that window belongs to.
_SESSION_DESTROYING_VERBS = frozenset({"join-pane", "move-pane", "break-pane"})


def _argv_strings() -> set[str]:
    """Every string literal the codec can put into an argv, ignoring docstrings and comments.

    An `ast` walk rather than a text scan, so the prose that explains *why* these verbs are
    rejected — including DEC-040's own reasoning, quoted in `swap_pane_args` — is free to name
    them. A rule enforced by grep cannot tell an argument from an argv.
    """
    tree = ast.parse(_CODEC.read_text(encoding="utf-8"))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef | ast.Module)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
    }


def test_the_codec_never_builds_a_command_that_can_destroy_a_managed_session() -> None:
    built = _argv_strings() & _SESSION_DESTROYING_VERBS

    assert built == set(), (
        f"the codec builds {sorted(built)}, which relocates a pane out of its window. A managed "
        "session's window left empty is a session tmux destroys, along with the identity marks "
        "on it — DEC-040's rejected alternative, probed rather than assumed. Exchange panes with "
        "`swap_pane_args` instead, which leaves both windows occupied."
    )


def test_the_check_can_see_the_argv_it_is_guarding() -> None:
    """Guards the test above from passing over an empty or docstring-only reading.

    A walk that found no argv strings at all would pass no matter what the codec built, which
    is the failure mode an AST-based check has and a grep does not.
    """
    strings = _argv_strings()

    assert "swap-pane" in strings, "the codec's own exchange verb was not seen as an argv string"
    assert "kill-pane" in strings or "list-panes" in strings
