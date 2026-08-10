"""A session's age reads in the unit a person would have used, on both surfaces.

`1440m ago` is a day, and nobody reads it as one. This is the test for the humanization —
and, as much, for the fact that there is now one implementation of it. Both surfaces carried
byte-identical copies before this task; the bot's had no test of its own, so a change to the
TUI's would have put the two out of agreement with nothing to say so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.relative_time import age

_NOW = datetime.now(UTC)


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(0), "0m ago"),
        (timedelta(seconds=59), "0m ago"),
        (timedelta(minutes=5), "5m ago"),
        (timedelta(minutes=59), "59m ago"),
        (timedelta(hours=1), "1h ago"),
        (timedelta(hours=5), "5h ago"),
        (timedelta(hours=23, minutes=59), "23h ago"),
        (timedelta(hours=24), "1d ago"),
        (timedelta(days=3), "3d ago"),
        (timedelta(days=90), "90d ago"),
    ],
)
def test_each_span_renders_in_its_own_unit(elapsed: timedelta, expected: str) -> None:
    assert age(datetime.now(UTC) - elapsed) == expected


def test_the_units_do_not_compose() -> None:
    """`5h ago`, never `5h 12m ago`.

    Not a style rule. The compound form would satisfy a reader while defeating this task's
    own gate check, which greps for a bare `m ago` surviving into an hours-or-days age — so
    the check would pass on exactly the output it exists to catch.
    """
    rendered = age(datetime.now(UTC) - timedelta(hours=5, minutes=12))

    assert rendered == "5h ago"
    assert "m ago" not in rendered


def test_a_record_stamped_in_the_future_reads_as_new_rather_than_negative() -> None:
    """Two hosts, two clocks. `-3m ago` reads as a broken session; `0m ago` as a new one."""
    assert age(datetime.now(UTC) + timedelta(minutes=3)) == "0m ago"


def test_both_surfaces_render_an_age_through_the_same_function() -> None:
    """The parity is structural — one function — rather than two files agreeing today.

    Asserted by identity, not by comparing two outputs: two implementations that happen to
    agree on the cases a test lists is exactly the state this task ended, and a
    same-output assertion would have passed throughout it.
    """
    from remote_agents.adapters.telegram import service as telegram_service
    from remote_agents.adapters.tui import model as tui_model

    assert tui_model.age is age
    assert telegram_service.age is age


def test_neither_surface_kept_a_private_copy() -> None:
    """The old private `_age` is gone from both adapters, not merely unused."""
    from remote_agents.adapters.telegram import service as telegram_service
    from remote_agents.adapters.tui import model as tui_model

    assert not hasattr(telegram_service, "_age")
    assert not hasattr(tui_model, "_age")
