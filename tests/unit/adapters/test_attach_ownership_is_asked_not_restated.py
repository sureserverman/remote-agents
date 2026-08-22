"""The attach-ownership rule is asked of the application, never restated by a frontend.

DEC-021 requires the two surfaces to offer a PRESERVED pane its attach **or neither to**, so
this is the one merged rule in this sub-plan where divergence is a correctness failure rather
than untidiness. It was written twice — inside `application/services.copy_attach` and again as
`_can_copy_attach` in the Telegram adapter — which is what let the bot inspect the same pane
twice to reach one answer.

**Why this file exists beside the `hasattr` check that was already there.** That check asserts
one identifier is absent, so a re-derivation under any other name passes it — `_pane_ok`, a
module-level helper, or an inline condition in `_detail_reply`. Every *other* merge in this
stage got a shape guard precisely because a returning duplicate does not come back wearing its
old name: the resume filter got an AST sweep because two of its three copies were anonymous
comprehensions, rename got a `set_label` sweep because "a second rename would not arrive called
`rename`", and the list-open pairing got a `refresh_readiness` sweep because it had never had a
name at all. The item whose decision makes divergence a correctness failure had the weakest
guard in the stage. A Tier-2 review found that, and it is the same lesson Stage 2 recorded.

**`live` and `preserved` are the sweep, and they are a good one because the rule cannot be
expressed without them.** Whatever a re-derivation is called and whatever shape it takes, it
has to ask whether the observed pane is live or preserved — that is the half DEC-021 is about.
Neither frontend reads either attribute today, which is what makes an exact-zero assertion
meaningful here rather than merely aspirational.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "remote_agents"
_FRONTENDS = {"telegram": _SRC / "adapters" / "telegram", "tui": _SRC / "adapters" / "tui"}

#: The two attributes of a `TerminalObservation` that only the ownership rule has a reason to
#: read. `project_id` and `profile_id` are deliberately **not** swept: both frontends compare
#: a chosen profile against a key for unrelated reasons, so a sweep over those names would
#: fail on code that has nothing to do with this rule and would be deleted the first time it
#: did.
_OWNERSHIP_ONLY_ATTRIBUTES = ("live", "preserved")


def _sources(tree_root: pathlib.Path) -> list[pathlib.Path]:
    assert tree_root.is_dir(), f"{tree_root} must exist or this sweep passes over nothing"
    return sorted(tree_root.rglob("*.py"))


@pytest.mark.parametrize("frontend", sorted(_FRONTENDS))
@pytest.mark.parametrize("attribute", _OWNERSHIP_ONLY_ATTRIBUTES)
def test_no_frontend_reads_the_pane_condition_the_rule_is_made_of(
    frontend: str, attribute: str
) -> None:
    """A frontend asking whether a pane is live or preserved is deriving the rule again."""
    offenders = [
        f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}"
        for path in _sources(_FRONTENDS[frontend])
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr == attribute
    ]

    assert offenders == [], (
        f"{frontend} reads observation.{attribute} — DEC-021's rule is "
        f"`application/session_actions.pane_is_attachable`, and it is asked, not restated: "
        f"{offenders}"
    )


def test_the_rule_is_defined_once_and_lives_in_application_policy() -> None:
    """One definition, beside the other lifecycle policy both surfaces share (DEC-001)."""
    definitions = [
        str(path.relative_to(_SRC.parent.parent))
        for path in sorted(_SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "pane_is_attachable"
    ]

    assert definitions == ["src/remote_agents/application/session_actions.py"], definitions


@pytest.mark.parametrize("frontend", sorted(_FRONTENDS))
def test_the_surfaces_that_ask_reach_the_shared_rule(frontend: str) -> None:
    """The complement of the sweeps above, so "nobody derives it" cannot pass by nobody using it.

    An exact-zero assertion is satisfied just as well by a surface that stopped offering attach
    altogether, which is why this stands beside them. The bot applies the rule directly for its
    row gate; the local surface reaches it through `copy_attach`, which is the whole of why it
    never needed a predicate of its own.
    """
    reaches = [
        path.name
        for path in _sources(_FRONTENDS[frontend])
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Name | ast.Attribute)
        and getattr(node, "id", getattr(node, "attr", None))
        in {"pane_is_attachable", "copy_attach"}
    ]

    assert reaches, f"{frontend} no longer asks anyone whether a pane may be attached"
