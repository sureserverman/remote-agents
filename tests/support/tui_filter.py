"""Wait for the project filter's debounced search, explicitly rather than by luck.

`ProjectsScreen` schedules its re-search on a `_FILTER_DEBOUNCE` timer, so the rows a test
reads immediately after typing are the rows from *before* the last keystroke. Several tests
that predate the debounce nevertheless kept passing, because the awaits between the final
keypress and the read sometimes exceed the delay — which is the shape of a test that passes
on an idle machine and fails on a loaded one, and is not a property worth relying on either
way.

So the wait is named and taken deliberately. Three times the delay rather than exactly one:
the timer fires *after* the interval, and the render it schedules then has to reach the
widget, so a wait of precisely the debounce is the one duration guaranteed to be marginal.
"""

from __future__ import annotations

from textual.pilot import Pilot

from remote_agents.adapters.tui.screens.launch import _FILTER_DEBOUNCE


async def settle_filter(pilot: Pilot[object]) -> None:
    """Let a pending filter search run, then let its render land."""
    await pilot.pause(_FILTER_DEBOUNCE * 3)
    await pilot.pause()
