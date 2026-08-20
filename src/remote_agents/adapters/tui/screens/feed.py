"""The notifications feed as a position of its own — the console's right-bottom pane.

The feed has lived as a region inside the dashboard since the durable table landed. Under
the three-pane console it is its own process, so it needs its own screen; what it renders is
unchanged, and is *moved* here rather than written a second time (DEC-037: the durable feed
carries the agent's words, rendered inert).
"""

from __future__ import annotations

from remote_agents.adapters.tui.screens.base import ChoiceScreen

#: The feed's one line when nothing has been observed — its DEC-009 answer.
NO_NOTIFICATIONS = "No notifications yet."


class FeedScreen(ChoiceScreen):
    """Newest-first observations, bounded; a glance, not an archive."""

    empty_state = NO_NOTIFICATIONS

    position = "FEED"
    can_refresh = True
    crumb = "Notifications"
    status = "What the agents on this host were last observed doing."

    async def populate(self) -> None:
        self.hide_entry()
        await self.reload()

    async def reload(self) -> None:
        """Filled in by Task 1.4, which moves the dashboard feed pane's render here."""
        self.show_choices(())
