"""The sentence the owner reads when a provider wiped its meters early — DEC-097.

`Claude limits were reset early — 5h 91% → 2%, week 64% → 0%`, and every part of that line is
somebody else's: the provider's own window labels, its own percentages, and a display name this
module is deliberately not allowed to know.

**Why the name is an argument rather than a lookup.** "Claude" spelled here would be a second
place this project names a provider, free to drift from the first (DEC-043, DEC-007). The
presenter therefore takes the resolved name and cannot spell one — a property a test can assert
over the module's own source, which is stronger than a convention.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from remote_agents.adapters.telegram.limit_reset_notifications import limit_reset_message
from remote_agents.application.limit_resets import EarlyReset

_AT = datetime(2026, 9, 18, 17, 0, tzinfo=UTC)


def _reset(label: str, before: float, after: float) -> EarlyReset:
    return EarlyReset(
        label=label,
        previous_percent=before,
        current_percent=after,
        expected_reset_at=_AT + timedelta(hours=1),
    )


def test_limit_reset_present_one_window_reads_as_the_owner_would_say_it() -> None:
    line = limit_reset_message("Claude", (_reset("5h", 91, 2),))

    assert line == "Claude limits were reset early — 5h 91% → 2%"


def test_limit_reset_present_two_windows_of_one_provider_are_one_sentence() -> None:
    """One message per provider, not per window (DEC-097): two windows moved in one event."""
    line = limit_reset_message("Claude", (_reset("5h", 91, 2), _reset("week", 64, 0)))

    assert line == "Claude limits were reset early — 5h 91% → 2%, week 64% → 0%"


def test_limit_reset_present_rounds_the_way_the_limits_block_already_rounds() -> None:
    """No second rounding rule: the shared `whole_percent` decides, here and in the block.

    A provider is free to publish `90.6`, and a figure that read `90.6%` here and `91%` two
    screens away would be two opinions about one number.
    """
    from remote_agents.application.session_views import whole_percent

    line = limit_reset_message("Codex", (_reset("5h", 90.6, 1.4),))

    assert f"{whole_percent(90.6)}%" in line and f"{whole_percent(1.4)}%" in line
    assert "90.6" not in line and "1.4" not in line


def test_limit_reset_present_makes_a_providers_own_label_safe() -> None:
    """DEC-014: the label is somebody else's text and reaches a markup-parsing surface.

    An unbalanced `<` in a window label is a rendering fault this project has already paid for
    once on another surface. The label is escaped at this boundary, where the markup is
    decided, rather than in `application/`, which takes no view on either surface's markup.
    """
    line = limit_reset_message("Claude", (_reset("<b>5h</b>", 91, 2),))

    assert "<b>" not in line
    assert "&lt;b&gt;5h&lt;/b&gt;" in line


def test_limit_reset_present_escapes_the_provider_name_it_is_handed_too() -> None:
    """The name is resolved elsewhere, so this boundary does not get to assume it is safe."""
    line = limit_reset_message("<i>Claude</i>", (_reset("5h", 91, 2),))

    assert "<i>" not in line


def test_limit_reset_present_spells_no_provider_name_of_its_own() -> None:
    """The structural half of "not a literal", asserted over the module's own source.

    A reviewer can be told the name is an argument; this makes it checkable. The day somebody
    adds a convenience mapping here, the second naming site exists and this fails.
    """
    import ast
    from pathlib import Path

    import remote_agents.adapters.telegram.limit_reset_notifications as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    # Docstrings are stripped, deliberately: the claim is about what the module *does*, and a
    # provider named in prose cannot drift into behaviour. The example sentence in this
    # module's own docstring is documentation, not a second lookup — and an earlier version of
    # this test, matching raw lines, could not tell the two apart.
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    spelled_in_code = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }
    for name in ("Claude", "Codex", "OpenCode", "Cursor Agent"):
        assert not any(name in text for text in spelled_in_code), (
            f"{name} is named in this module's code as well as in the registry"
        )


def test_limit_reset_present_says_nothing_when_nothing_reset() -> None:
    """A caller that asks with an empty tuple gets no sentence rather than a headline."""
    assert limit_reset_message("Claude", ()) == ""
