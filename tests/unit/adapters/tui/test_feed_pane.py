"""The notifications feed renders newest first, bounded, inert — on both of its surfaces.

Every assertion here runs twice: once against the feed **region** inside the combined
dashboard, which is what `remote-agents tui` still shows in a bare terminal, and once
against the standalone feed **pane**, which is the console's right-bottom process. The two
share one implementation (`screens/feed.py`'s `FeedRegion`), and parametrizing rather than
copying is what keeps that true — a render that drifted on one surface would have to fail
here on the other.

The pane is a reader of `agent_activity` (via the composition's `activity_feed`
capability) and nothing else — it never drains the spool, because consuming spool files
would starve the phone's notifications (task 5.2's correction). Text an agent produced
travels through a `markup=False` Static, the same inertness rule every row sink in this
surface follows (DEC-014's spirit: text this app did not author is displayed, never
interpreted).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backends import backend_for
from textual.widgets import Static

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.panes import FeedPane
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


class _Launcher:
    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple:
        return ()


def _activity(kind: ActivityKind, *, minutes_ago: int, detail: str | None = None):
    return AgentActivity(
        "01234567-89ab-cdef-0123-456789abcdef",
        kind,
        detail,
        datetime.now(UTC) - timedelta(minutes=minutes_ago),
        ActivityConfidence.REPORTED,
    )


def _context(feed=None) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=_Launcher(),  # type: ignore[arg-type]
            projects=_Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
            activity_feed=feed,
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


#: The two surfaces the feed renders on. `RemoteAgentsTui` rests on the dashboard, whose
#: feed is one region of three; `FeedPane` rests on the feed and nothing else. Both draw
#: into a `#feed-pane` Static, which is what lets one set of assertions cover both.
_SURFACES = pytest.mark.parametrize(
    "surface", (RemoteAgentsTui, FeedPane), ids=("dashboard-region", "standalone-pane")
)


@_SURFACES
async def test_the_feed_renders_newest_first_from_the_capability(surface) -> None:
    async def feed() -> tuple[AgentActivity, ...]:
        return (
            _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail="May I push?"),
            _activity(ActivityKind.COMPLETED, minutes_ago=5),
        )

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#feed-pane", Static)
        text = str(pane.content)
        assert "May I push?" in text
        assert text.index("May I push?") < text.index("finished"), "newest renders first"


@_SURFACES
async def test_an_absent_capability_keeps_the_placeholder(surface) -> None:
    app = surface(_context(None))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#feed-pane", Static)
        assert "No notifications yet." in str(pane.content)


@_SURFACES
async def test_an_empty_feed_says_so_rather_than_going_blank(surface) -> None:
    async def feed() -> tuple[AgentActivity, ...]:
        return ()

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "No notifications yet." in str(app.screen.query_one("#feed-pane", Static).content)


@_SURFACES
async def test_hostile_text_is_rendered_inert(surface) -> None:
    async def feed() -> tuple[AgentActivity, ...]:
        return (
            _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=0, detail="[link=https://x][bold]t[/"),
        )

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(app.screen.query_one("#feed-pane", Static).content)
        assert "[link=" in text, "markup must be displayed, never interpreted"


@_SURFACES
async def test_a_raising_feed_keeps_the_pane_and_its_surface_standing(surface) -> None:
    async def feed() -> tuple[AgentActivity, ...]:
        raise RuntimeError("store contended")

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running
        assert "No notifications yet." in str(app.screen.query_one("#feed-pane", Static).content)


@_SURFACES
async def test_flash_fires_once_per_new_observation_batch_and_never_on_first_load(surface) -> None:
    """The first load is history, not news — replaying it onto the status line would
    flash the owner for things they were already told. After that, one flash per batch
    that actually contains something new, and silence for an unchanged feed."""
    feed_rows: list[tuple[AgentActivity, ...]] = [
        (_activity(ActivityKind.COMPLETED, minutes_ago=5),),
    ]
    flashes: list[str] = []

    async def feed() -> tuple[AgentActivity, ...]:
        return feed_rows[0]

    async def flash(text: str) -> None:
        flashes.append(text)

    from dataclasses import replace

    context = replace(_context(feed), console_flash=flash)
    app = surface(context)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert flashes == [], "history must not flash"

        feed_rows[0] = (
            _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=0, detail="May I push?"),
            *feed_rows[0],
        )
        await app.screen._reload_feed()
        await pilot.pause()
        assert len(flashes) == 1
        assert "waiting for an answer" in flashes[0]

        await app.screen._reload_feed()
        await pilot.pause()
        assert len(flashes) == 1, "an unchanged feed must not flash again"


@_SURFACES
async def test_the_feed_is_bounded_rather_than_an_archive(surface) -> None:
    """ "A glance, not an archive" was asserted in prose and never driven.

    The reader LIMITs and the render slices, both at `FEED_LIMIT` — but every test here fed
    two rows, so a bound of twenty and a bound of two thousand were indistinguishable.
    """
    from remote_agents.adapters.tui.context import FEED_LIMIT

    async def feed() -> tuple[AgentActivity, ...]:
        return tuple(
            _activity(ActivityKind.COMPLETED, minutes_ago=minute)
            for minute in range(FEED_LIMIT + 7)
        )

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(app.screen.query_one("#feed-pane", Static).content)
        assert len(text.splitlines()) == FEED_LIMIT


async def test_the_feed_pane_offers_no_flows_at_all() -> None:
    """It advertised "Add project" and honoured it, pushing the project wizard into the
    notifications pane — where escape returns to a feed. The narrowest, most read-only
    surface in the console offers nothing that starts a session."""
    app = FeedPane(_context(None))
    async with app.run_test() as pilot:
        await pilot.pause()
        offered = set(app.screen.active_bindings)
        assert {"ctrl+n", "ctrl+o", "ctrl+s"}.isdisjoint(offered), offered

        await app.action_add_project()
        await pilot.pause()
        assert app.screen.position == "FEED", "a declined flow must not move the pane"
