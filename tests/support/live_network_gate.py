"""Whether a run asked for the `live_network` tests by name.

Kept apart from `conftest.py` so the rule can be tested: a gate that lets the owner's login
reach a provider's server un-asked is not one to leave unpinned.
"""

from __future__ import annotations

from _pytest.mark.expression import Expression

MARKER = "live_network"


def asked_for(markexpr: str | None) -> bool:
    """Whether `-m <markexpr>` selects a test *because* it carries the marker.

    True when the expression holds for a test marked only `live_network` and fails for a test
    with no marker at all. So `live_network` and `live_network or slow` ask; an empty
    expression, `not slow` and `not (slow and live_network)` do not, though each would select
    the test. A substring test would take the last of those for a request.
    """
    if not markexpr:
        return False
    try:
        expression = Expression.compile(markexpr)
    except SyntaxError:
        return False
    return expression.evaluate(_only(MARKER)) and not expression.evaluate(_only(None))


def _only(marker: str | None):
    def matcher(name: str, /, **_kwargs: object) -> bool:
        return name == marker

    return matcher
