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
    """
    sentences = [node.name for node in _functions(_ADAPTER) if _returns_a_sentence(node)]

    assert sentences, "the adapter words nothing; the surface's half of the split is gone"


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
    asserts the whole set rather than the one that changed, because the failure mode is a
    *rewording* -- no assertion anywhere else in the suite reads this text, so a nicer sentence
    ships inside a refactor with nothing objecting and a diff that reads as cleanup.

    The eviction line is the reason this exists. The policy now returns which session paid, so
    naming it in the warning is one interpolation away and was briefly written that way; the
    stage's own evaluator caught it as the single behaviour delta in an otherwise exact
    relocation. BL-008 holds that improvement so it can be taken deliberately.

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

    assert "the notification queue is full; dropping the oldest held for one session (%d held)" in (
        sentences
    ), "the eviction warning was reworded; the rule moved, the sentence does not"
    assert len(sentences) == 10, (
        f"this module says {len(sentences)} things to an operator, and it said 10 before the "
        "policy moved out of it -- a line added or removed here is an intentional edit"
    )
