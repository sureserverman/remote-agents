"""Transitional host for the `Step` positions this stage has not extracted yet.

**Deleted by Task 2.4, together with `Step` itself.** It exists so that every intermediate
commit in Stage 2 has exactly one mechanism *per position* — a real screen for what has
moved, this single host for what has not — rather than two mechanisms fighting over the same
widgets. The stage's Rollback note calls the half-extracted state worse than either whole,
and this is what keeps it from being that.

It renders nothing of its own: whichever legacy `_show_*` on the app pushed it also fills it.
"""

from __future__ import annotations

from remote_agents.adapters.tui.screens.base import ChoiceScreen


class LegacyScreen(ChoiceScreen):
    """A blank body the app's remaining `Step` handlers paint into."""

    async def choose(self, key: str) -> None:
        await self.tui.legacy_choose(key)
