"""Bundling a pass's observations is asked of the application, never restated by the adapter.

The delivery driver decides *when* to send and *what words* to use. It does not decide which
observations belong together, which copy of a repeated one survives, or how many lines a
message spells out -- those are `application/notification_policy`'s, and this file is what
stops them growing back here.

**Swept for shape, not for names (DEC-043).** A re-derivation does not come back wearing
`grouped_for_delivery`'s name, so a sweep for the identifier would pass over the exact thing it
exists to catch. DEC-043 accepted cost 2 records the measurement behind that: in Sub-plan 2 a
definition-sweep covered two of three merged rules and missed the third for having been an
anonymous comprehension, and the stage after it found three further shapes a name check cannot
see. So the two sweeps below look for the operations the rules cannot be expressed without.

- **Deciding which copy is newer** cannot avoid *comparing or ordering* `observed_at`. Reading
  it is fine and stays -- `_moment` renders the stamp into a sentence, which is the surface's
  job -- so the sweep is scoped to comparisons and sort keys rather than to the attribute.
- **Taking the newest N** cannot avoid a negative slice. That is `shown_in_message` and the
  layout half of `for_update`, both of which now take the budget as an argument precisely so
  the adapter keeps the number and gives up the rule (DEC-034 accepted cost 4).

The third test is the other half of "asked": a sweep that only forbids can be satisfied by
deleting the behaviour, so one case asserts the driver actually reaches the policy module.
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[4] / "src" / "remote_agents"
_ADAPTER = _SRC / "adapters" / "telegram"
_POLICY = "remote_agents.application.notification_policy"


def _sources() -> list[pathlib.Path]:
    """Every module in the adapter, with the emptiness this sweep would otherwise pass over."""
    assert _ADAPTER.is_dir(), f"{_ADAPTER} must exist or these sweeps run over nothing"
    found = sorted(_ADAPTER.rglob("*.py"))
    assert found, f"{_ADAPTER} holds no modules; the sweeps below would pass vacuously"
    return found


def _nodes():
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            yield path, node


def _where(path: pathlib.Path, node: ast.AST) -> str:
    return f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}"


def _reads_observed_at(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Attribute) and inner.attr == "observed_at" for inner in ast.walk(node)
    )


def test_the_adapter_never_orders_or_compares_observations_by_when_they_happened() -> None:
    """Which copy of a repeated observation survives is the collapse, and the collapse moved.

    A `Compare` reaching `observed_at` is "is this one newer"; a `Lambda` reaching it is a sort
    key. Both are the collapse being re-derived. Rendering the stamp is neither and is left
    alone, which is why this looks at the two node kinds rather than at the attribute.
    """
    offenders = [
        _where(path, node)
        for path, node in _nodes()
        if isinstance(node, ast.Compare | ast.Lambda) and _reads_observed_at(node)
    ]

    assert offenders == [], f"the adapter is deciding observation order itself: {offenders}"


def test_the_adapter_never_takes_the_newest_n_of_anything() -> None:
    """The line budget is the adapter's number; spending it is the policy's rule.

    A negative lower bound is what "the newest that fit" cannot be written without, and it is
    the operation that made `shown_in_message` and `for_update` two owners of one answer before
    they were one function.
    """
    offenders = [
        _where(path, node)
        for path, node in _nodes()
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Slice)
        and isinstance(node.slice.lower, ast.UnaryOp)
        and isinstance(node.slice.lower.op, ast.USub)
    ]

    assert offenders == [], f"the adapter is selecting what a message spells out: {offenders}"


def test_the_delivery_driver_asks_the_policy_module() -> None:
    """The positive half, because a forbidding sweep is also satisfied by deleting the feature.

    Named on the module rather than on the functions: which of them the driver needs is allowed
    to change, that it reaches the one place holding them is not.
    """
    source = (_ADAPTER / "notifications.py").read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert _POLICY in imported, (
        f"the notifier does not ask the policy module; it imports {imported}"
    )


# The suppression window --------------------------------------------------------------------


def test_the_adapter_never_doubles_anything() -> None:
    """The taper is DEC-013 clause (5), and it moved. Exponentiation is what it cannot avoid.

    Swept for the operator rather than for `window`'s name (DEC-043), because a re-derivation
    comes back as `self._rate_limit * 2 ** n` inline, or as a fresh helper, and neither wears
    the old identifier. `<<` is swept alongside `**` because `rate_limit * (1 << n)` is the
    same taper written by someone avoiding a float, and the stage's evaluator pointed out that
    the first version of this sweep let it through.

    DEC-031 is the reason this is worth a guard at all: the failure here was
    measured at 96 messages over eight hours where the taper intends twelve, and it was
    invisible in every unit test that checked a single doubling.
    """
    offenders = [
        _where(path, node)
        for path, node in _nodes()
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow | ast.LShift)
    ]

    assert offenders == [], f"the adapter is computing a backoff itself: {offenders}"


def test_the_adapter_never_stamps_a_repeat_count() -> None:
    """Writing a `Sent` is the counter bookkeeping, and the reset rule rides on it.

    The adapter still *owns* the map -- residence is not policy, the same split DEC-026 makes
    for the backlog -- so it reads entries freely. What it may not do is decide what goes in
    one, because that decision is where "a different kind resets the session" lives, and a
    second copy of it would be a second answer to when the owner stops being told.

    Two sweeps, because the first is a name check and the stage's evaluator was right that a
    name check does not hold: a re-derivation storing `(moment, count)` as a plain tuple, or as
    its own dataclass, walks straight past `Sent(`. The second is representation-independent --
    it forbids *writing into* `_last_sent` at all, whatever the value happens to be.

    **Disclosed limit.** Neither sweep catches a re-derivation that keeps its own parallel map
    under a different name and never touches `_last_sent`. That shape is not expressible as a
    sweep over this module without also forbidding every ordinary dictionary in it, so it is
    named here rather than left to look covered.
    """
    constructions = [
        _where(path, node)
        for path, node in _nodes()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Sent"
    ]
    writes = [
        _where(path, node)
        for path, node in _nodes()
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "_last_sent"
    ]

    assert constructions == [], f"the adapter is stamping repeat counts itself: {constructions}"
    assert writes == [], f"the adapter is writing into the suppression map itself: {writes}"


def test_the_adapter_never_ages_an_entry_against_a_horizon() -> None:
    """Retention and dueness both reduce to "how old is this entry", and both moved.

    `sent_at` is what neither can be written without: a horizon is a comparison against it, and
    `due` is the same comparison with a different threshold. The adapter still *holds* the map
    -- residence is not policy -- and passes it whole, so it never needs to read inside an
    entry. The moment it does, the count-independent floor DEC-031 records has a second
    implementation, and that one will be the proportional horizon again, because the
    proportional horizon is the obvious thing to write.
    """
    offenders = [
        _where(path, node)
        for path, node in _nodes()
        if isinstance(node, ast.Attribute) and node.attr in {"sent_at", "repeats"}
    ]

    assert offenders == [], f"the adapter is reading inside a suppression entry: {offenders}"
