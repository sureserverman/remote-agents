"""No agent's dialog wording is a string constant outside that agent's own package.

DEC-070's claim, for the values this plan moved: a provider-discriminating token belongs to the
provider's vertical, so a fifth agent becomes answerable by editing its own package and nothing
else. The wordings used to be module constants in `adapters/tmux/trust.py`, which is exactly why
only Claude could be answered — there was nowhere for a second agent's dialog to be declared.

**Derived from what the verticals declare, not from a list of words.** A hard-coded word list
would be a second copy of the very thing this asserts is single-copy, and it would go stale the
first time a vendor reworded a dialog. This reads today's declarations and looks for them.

**Written as an AST check because the gate check it replaces was a `grep`, and greps read
prose.** `! grep -rn 'Yes, I trust this folder\\|No, exit\\|...' src/` matches five docstrings and
comments that *discuss* the dialogs — including the paragraph explaining why a bare Enter into
Claude's 2.1.263 layout answers "No, exit" — and reports a defect that is not one. That is the
third check of this shape in this master to fail on prose; the other two are now tests too.
"""

from __future__ import annotations

import ast
import pathlib

_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_VERTICALS = "adapters/agents/"

#: The one module outside the verticals that spells two of these strings, named rather than
#: excluded silently — **BL-052**.
#:
#: `_READINESS_BLOCKERS` carries codex's question and cursor-agent's `Workspace Trust Required`
#: because a readiness blocker is a *different fact* from a dialog: it is what the agent prints
#: while it is not working, which for `claude` is the pre-trust screen and not the dialog at
#: all. Folding one into the other is a design question, not a move, so this check reports the
#: duplication through the backlog rather than either failing over it or pretending it is not
#: there. Removing this exception is what closing BL-052 looks like.
_NAMED_EXCEPTION = "adapters/tmux/profiles.py"


def _declared_wordings() -> dict[str, set[str]]:
    """Every string a vertical declares about its own dialog, by profile."""
    from remote_agents.adapters.agents.registry import provider_descriptors

    wordings = {}
    for descriptor in provider_descriptors():
        dialog = descriptor.trust_dialog
        if dialog is None:
            continue
        wordings[str(descriptor.profile_id)] = {
            dialog.question,
            dialog.affirmative,
            dialog.negative,
            dialog.identifies_by,
        }
    return wordings


def _string_constants(path: pathlib.Path) -> set[str]:
    """String *values* in one module, with docstrings excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
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
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings
    }


def test_the_named_exception_is_still_the_only_one_and_still_earns_its_name() -> None:
    """An exception nobody re-reads becomes a hole. This asserts it is still exactly what it says.

    If `profiles.py` stops spelling these strings, BL-052 is closed and the exception must go —
    a stale allowance is how a check quietly stops checking the thing it names.
    """
    constants = _string_constants(_SOURCE / "adapters" / "tmux" / "profiles.py")
    declared = {wording for strings in _declared_wordings().values() for wording in strings}

    assert constants & declared, (
        "adapters/tmux/profiles.py no longer spells any agent's dialog wording, so BL-052 is "
        "closed: delete `_NAMED_EXCEPTION` and this test, and let the sweep cover that file"
    )


def test_no_dialog_wording_is_spelled_outside_the_vertical_that_declares_it() -> None:
    wordings = _declared_wordings()
    assert wordings, "no vertical declares a dialog; this check would pass vacuously"

    offenders = []
    for path in _SOURCE.rglob("*.py"):
        relative = str(path.relative_to(_SOURCE))
        if relative.startswith(_VERTICALS) or relative == _NAMED_EXCEPTION:
            continue
        constants = _string_constants(path)
        for profile, strings in wordings.items():
            for wording in strings & constants:
                offenders.append(f"{relative}: {profile}'s {wording!r}")

    assert offenders == [], (
        "an agent's dialog wording is a string constant outside its own vertical, so a fifth "
        "provider could not be made answerable by editing only its own package (DEC-070):\n"
        + "\n".join(offenders)
    )


def test_the_parser_holds_no_agent_specific_string_of_its_own() -> None:
    """The narrower half, on the module this plan emptied — and where a relapse would start.

    `classify_trust_capture` and `plan_trust_keys` take the dialog they read. A constant
    creeping back here is how the module became Claude-only the first time: it reads perfectly
    well, every Claude test stays green, and the second agent quietly stops being answered.
    """
    constants = _string_constants(_SOURCE / "adapters" / "tmux" / "trust.py")
    declared = {wording for strings in _declared_wordings().values() for wording in strings}

    assert not (constants & declared), (
        f"the trust parser spells an agent's own dialog again: {sorted(constants & declared)}"
    )
