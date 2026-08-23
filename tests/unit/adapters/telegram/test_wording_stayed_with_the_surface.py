"""The policy answers; the surface says. This is the line, swept from both sides.

DEC-043: *"a shared function therefore **signals** rather than **words** -- it returns a value,
a `None`, a predicate -- and never a sentence."* `application/notification_policy` holds the
decisions about which observations belong together and which of them a message spells out;
`adapters/telegram/notifications` holds the words, the line budget, and the keyboard. DEC-034
accepted costs 3 and 4 are the two halves that must not follow the policy across: the fold into
"and N earlier", and an amendment's obligation to carry its keyboard.

**Both directions are swept, because either one alone is satisfiable by deleting things.** A
sweep that only forbids sentences in the policy module passes over a policy module that has
absorbed the rendering *and* over one where the rendering no longer exists. So the second case
asserts the adapter still words things and still holds the number it spends.

**The shape is the return type, not a name.** A re-derivation arrives under a fresh identifier,
which is what makes a name check the wrong instrument (DEC-043 accepted cost 2). What it cannot
arrive without is handing back a `str`: that is the whole of what "a sentence" means here, and
it is why a signal is a tuple, a bool or a `None` in every function on the policy side.

Mutation-tested when written rather than asserted to work: `activity_text` was moved into the
policy module, the first case below failed on it, and it was moved back. A guard that has never
been seen to fail is a guard nobody has checked.
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[4] / "src" / "remote_agents"
_POLICY = _SRC / "application" / "notification_policy.py"
_ADAPTER = _SRC / "adapters" / "telegram" / "notifications.py"

#: Every sentence this module said to an operator at `0e334c2`, the commit this sub-plan began
#: from. Written out rather than read back from git, so this is a claim about what the module
#: says today and not the tree being compared with itself.
_AT_THE_BASE = frozenset(
    {
        "a notification the owner pressed was not this session's current one; "
        "the standing message is kept",
        "an activity notification was sent without its Open session button",
        "could not ask which notified sessions have finished",
        "could not deliver an activity notification; holding it for retry",
        "could not move the live view below the notifications",
        "could not remove the notification a replacement supersedes",
        "could not remove the notification of a session that has finished",
        "dropping an activity this service will not speak about",
        "giving up on an activity notification after %d refusals; dropping %d "
        "observation(s) for session %s",
        "holding %d undelivered notification(s) in memory; a restart now loses them",
        "the notification queue is full; dropping the oldest held for session %s (%d held)",
    }
)


def _functions(path: pathlib.Path) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    assert path.is_file(), f"{path} must exist or this sweep runs over nothing"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert found, f"{path} defines no functions; the sweeps below would pass vacuously"
    return found


def _returns_a_sentence(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this function hands back words: `str`, `str | None`, or `Optional[str]`.

    Widened past a bare `-> str` at the Stage 1 gate, where the Tier-2 review pointed out that
    `str | None` is exactly the shape a half-hearted re-derivation of the wording would take --
    a renderer that returns the sentence or `None` when there is nothing to say.

    Deliberately **top-level only**. `dict[str, int]` keyed by session id is not a sentence, and
    a sweep that flagged any nested `str` would be one this module could not honestly satisfy.
    """
    return _is_str(node.returns)


def _is_str(annotation: ast.expr | None) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "str"
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_str(annotation.left) or _is_str(annotation.right)
    if (
        isinstance(annotation, ast.Subscript)
        and isinstance(annotation.value, ast.Name)
        and annotation.value.id == "Optional"
    ):
        return _is_str(annotation.slice)
    return False


def test_no_policy_function_ever_returns_a_sentence() -> None:
    """A signal is a bundle, a tuple of kinds, a selection. It is never words.

    The bot sizes its wording for a chat message and a second frontend would size it for a
    full-screen pane, so a sentence reached through a shared module is one surface's voice
    arriving on the other -- a functionality change wearing a refactor's clothes, and invisible
    to every behavioural test because both surfaces stay consistent with themselves.
    """
    wording = [
        f"{node.name}:{node.lineno}" for node in _functions(_POLICY) if _returns_a_sentence(node)
    ]

    assert wording == [], f"the policy module is wording things: {wording}"


def test_the_adapter_still_owns_the_wording() -> None:
    """The other direction, because "no sentences over there" is also true of no sentences at all.

    DEC-034 accepted cost 3: the amendment's keyboard-carrying obligation is presentation and
    stays here. If this ever reads zero, the wording did not stay with the surface -- it left.

    **Named, not counted.** A bare "at least one function returns a string" was satisfied by
    `_mint`, which returns `str | None` and is a token, not a sentence -- the close-out
    evaluator rewrote every renderer's annotation to `-> object:` and this still passed on the
    minter alone. So the renderers are named. They are the things DEC-043 says must not follow
    the policy across, and naming them is the only way this case can fail for its own reason.
    """
    wording = {node.name for node in _functions(_ADAPTER) if _returns_a_sentence(node)}

    assert {"activity_text", "_sentence"} <= wording, (
        "the adapter stopped wording things; the renderers named here are the surface's half "
        f"of DEC-043's split and must stay. Found: {sorted(wording)}"
    )


def test_the_line_budget_is_the_adapters_number() -> None:
    """DEC-034 accepted cost 4: the fold is presentation, and so is the number driving it.

    Asserted as a module-level assignment here rather than as an argument over there, because
    the two claims fail differently: the policy module acquiring a default is caught by
    `tests/unit/application/test_notification_policy.py`, and the constant *leaving* this
    module -- which would make the cap-driven-down test in `test_notifications.py` patch
    something nothing reads -- is caught here.
    """
    assigned = {
        target.id
        for node in ast.walk(ast.parse(_ADAPTER.read_text(encoding="utf-8")))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert "_MAXIMUM_LINES_PER_MESSAGE" in assigned, (
        "the line budget left the surface; the cap-driven-down test now patches nothing"
    )


def test_every_operator_facing_sentence_is_the_one_it_has_always_been() -> None:
    """DEC-043 accepted cost 1: a merged rule's words are pinned, because nothing else sees them.

    The rules for grouping, the taper, retention and the backlog all moved to
    `application/notification_policy` across this sub-plan. None of the sentences did. This
    compares the whole set, because the failure mode is a *rewording* -- no assertion anywhere
    else in the suite reads this text, so a nicer sentence ships inside a refactor with nothing
    objecting and a diff that reads as cleanup.

    **Set equality, and the first version of this test did not have it.** It asserted that the
    eviction sentence was present and that the count was still ten -- which pins one of the ten
    and leaves the other nine held by a number. The close-out evaluator reworded a different
    line, changing one word's case, and every case here passed. That is the same defect the
    Stage 1 gate fixed one file over and described as a check overstating its coverage, so the
    lesson is evidently cheaper to state than to learn: a guard whose docstring names a set has
    to compare the set.

    The eviction line is the reason this exists, and it is also the one line that has since
    changed. The policy returns which session paid, so naming it was one interpolation away and
    was briefly written that way during the relocation; the stage's own evaluator caught it as
    the single behaviour delta in an otherwise exact move, and it was reverted so it could be
    taken on its own. It was, closing BL-032 -- and taking it meant editing the set below by
    hand, which is exactly the deliberate act this pin exists to force. The guard worked: the
    improvement happened, and it happened in a commit about itself.

    Pinned as an exact set, so adding a line fails here too and has to be an intentional edit.
    """
    sentences = {
        node.args[0].value
        for node in ast.walk(ast.parse(_ADAPTER.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"warning", "info", "error", "debug", "exception"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }

    assert sentences == _AT_THE_BASE, (
        "an operator-facing sentence changed. The rules moved out of this module; the words "
        f"did not.\n  gone: {sorted(_AT_THE_BASE - sentences)}"
        f"\n  new:  {sorted(sentences - _AT_THE_BASE)}"
    )
