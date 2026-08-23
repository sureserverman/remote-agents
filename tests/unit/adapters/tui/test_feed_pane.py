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
from textual.widgets import OptionList

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


def _feed_pane(app) -> OptionList:
    return app.screen.query_one("#feed-pane", OptionList)


def _feed_rows(app) -> list[str]:
    """The prompts actually drawn, in order. The pane is a list now, not a paragraph."""
    pane = _feed_pane(app)
    return [str(pane.get_option_at_index(i).prompt) for i in range(pane.option_count)]


def _feed_text(app) -> str:
    """Every drawn row joined, so assertions written against the old Static still read
    naturally. Newline-joined rather than space-joined on purpose: the bound test counts
    `splitlines()`, and one option is one line."""
    return "\n".join(_feed_rows(app))


#: The two surfaces the feed renders on. `RemoteAgentsTui` rests on the dashboard, whose
#: feed is one region of three; `FeedPane` rests on the feed and nothing else. Both draw
#: into a `#feed-pane` OptionList, which is what lets one set of assertions cover both.
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
        text = _feed_text(app)
        assert "May I push?" in text
        assert text.index("May I push?") < text.index("finished"), "newest renders first"


@_SURFACES
async def test_an_absent_capability_keeps_the_placeholder(surface) -> None:
    app = surface(_context(None))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "No notifications yet." in _feed_text(app)


@_SURFACES
async def test_an_empty_feed_says_so_rather_than_going_blank(surface) -> None:
    async def feed() -> tuple[AgentActivity, ...]:
        return ()

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "No notifications yet." in _feed_text(app)


@_SURFACES
async def test_hostile_text_is_rendered_inert(surface) -> None:
    async def feed() -> tuple[AgentActivity, ...]:
        return (
            _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=0, detail="[link=https://x][bold]t[/"),
        )

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        text = _feed_text(app)
        assert "[link=" in text, "markup must be displayed, never interpreted"


@_SURFACES
async def test_a_raising_feed_keeps_the_pane_and_its_surface_standing(surface) -> None:
    async def feed() -> tuple[AgentActivity, ...]:
        raise RuntimeError("store contended")

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running
        assert "No notifications yet." in _feed_text(app)


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
        text = _feed_text(app)
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


# The pane is a list, not a paragraph -----------------------------------------------------------


@_SURFACES
async def test_the_feed_pane_is_a_scrollable_option_list(surface) -> None:
    """The pane held a `Static`, which cannot scroll and cannot highlight, so the twenty-first
    observation was simply unreachable. An `OptionList` scrolls and highlights, and it inherits
    `ChoiceScreen.on_option_list_option_selected`'s routing for free."""

    async def feed() -> tuple[AgentActivity, ...]:
        return (_activity(ActivityKind.COMPLETED, minutes_ago=1),)

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        assert isinstance(pane, OptionList)
        # Inertness is asserted behaviourally by `test_hostile_text_is_rendered_inert`, not by
        # reading a flag: `OptionList` takes `markup=` and exposes no public attribute for it,
        # so a check here would have to reach for `_markup` and would pass just as happily on
        # a widget that had stopped honouring it.
        assert pane.option_count == 1


@_SURFACES
async def test_a_read_of_n_observations_draws_n_rows(surface) -> None:
    async def feed() -> tuple[AgentActivity, ...]:
        return tuple(_activity(ActivityKind.COMPLETED, minutes_ago=minute) for minute in range(6))

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _feed_pane(app).option_count == 6


@_SURFACES
async def test_a_row_id_is_stable_across_reloads_and_unique_per_observation(surface) -> None:
    """The id is what Enter comes back as, so it must name the observation and nothing else.

    Composite of session, kind and observed_at rather than the index: the feed is newest-first
    and grows at the head, so an index-keyed row means the row under the cursor becomes a
    different notification every time an agent reports.
    """
    # Built once, outside the reader: `_activity` stamps `datetime.now(UTC)` on every call, so
    # a reader that rebuilt them would mint a new observed_at per reload and this test would
    # be asserting the clock rather than the key.
    observations = (
        _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1),
        _activity(ActivityKind.COMPLETED, minutes_ago=5),
    )

    async def feed() -> tuple[AgentActivity, ...]:
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        first = [pane.get_option_at_index(i).id for i in range(pane.option_count)]
        assert len(set(first)) == len(first), "two observations must not share a row id"
        assert all(key.startswith("notification:") for key in first)
        await app.screen._reload_feed()
        await pilot.pause()
        pane = _feed_pane(app)
        again = [pane.get_option_at_index(i).id for i in range(pane.option_count)]
        assert again == first, "the same observation must keep the same id across a reload"


@_SURFACES
async def test_a_reload_keeps_the_cursor_on_the_row_it_was_on(surface) -> None:
    """The pane repaints every 10 seconds. A cursor that jumped home on each tick would make
    the list unusable for the one thing it is now for -- reading down it."""
    rows = [
        (
            _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail="one"),
            _activity(ActivityKind.COMPLETED, minutes_ago=5, detail="two"),
            _activity(ActivityKind.QUIET, minutes_ago=9, detail="three"),
        )
    ]

    async def feed() -> tuple[AgentActivity, ...]:
        return rows[0]

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        pane.highlighted = 2
        held = pane.get_option_at_index(2).id

        # A newer observation arrives at the head, pushing every held row down one.
        rows[0] = (_activity(ActivityKind.LIMIT_REACHED, minutes_ago=0, detail="new"), *rows[0])
        await app.screen._reload_feed()
        await pilot.pause()

        pane = _feed_pane(app)
        assert pane.get_option_at_index(pane.highlighted).id == held, (
            "the cursor must follow its observation, not its index"
        )
        assert pane.highlighted == 3


@_SURFACES
async def test_a_cursor_whose_row_aged_out_rests_on_the_first_row(surface) -> None:
    """Same resting rule every other list on this surface keeps (DEC-007): somewhere
    deliberate and non-mutating, never nowhere."""
    rows = [
        (
            _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail="one"),
            _activity(ActivityKind.COMPLETED, minutes_ago=5, detail="two"),
        )
    ]

    async def feed() -> tuple[AgentActivity, ...]:
        return rows[0]

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        _feed_pane(app).highlighted = 1

        rows[0] = (_activity(ActivityKind.QUIET, minutes_ago=0, detail="only"),)
        await app.screen._reload_feed()
        await pilot.pause()

        assert _feed_pane(app).highlighted == 0


@_SURFACES
async def test_the_empty_state_is_a_row_the_cursor_cannot_act_on(surface) -> None:
    """DEC-009: the pane declares it can be empty and says so in its own words. As a row now
    rather than a paragraph -- and disabled, so the cursor cannot rest on a sentence and
    answer Enter with nothing."""

    async def feed() -> tuple[AgentActivity, ...]:
        return ()

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        assert pane.option_count == 1
        assert "No notifications yet." in str(pane.get_option_at_index(0).prompt)
        assert pane.get_option_at_index(0).disabled is True


# A row says whose notification it is ----------------------------------------------------------


def _named_activity(
    kind, *, minutes_ago: int, detail=None, session="01234567-89ab-cdef-0123-456789abcdef"
):
    from datetime import timedelta

    return AgentActivity(
        session,
        kind,
        detail,
        datetime.now(UTC) - timedelta(minutes=minutes_ago),
        ActivityConfidence.REPORTED,
    )


def _index_context(feed, records):
    """A context whose store reports `records`, and the fake store itself.

    Returned as a pair so a caller can assert what the feed's read did *not* do.
    """
    from dataclasses import replace as _replace

    class _Sessions:
        #: Counted, not refused. The *dashboard* legitimately refreshes readiness for its own
        #: sessions pane, so a fake that raised would fail that surface for doing its job --
        #: it did, on the first run of these cases. What must not happen is the *feed's* read
        #: probing tmux, and that is asserted by calling `_reload_feed` on its own and
        #: checking this counter has not moved.
        refreshed = 0

        async def refresh_readiness(self):
            type(self).refreshed += 1
            return records

        async def list_sessions(self):
            return records

    _Sessions.refreshed = 0

    context = _context(feed)
    sessions = _Sessions()
    return _replace(context, backend=_replace(context.backend, sessions=sessions)), sessions


def _session_record(session_id: str, slug: str = "opaque-existing"):
    from remote_agents.domain.models import (
        ProfileId,
        ProjectId,
        SessionDisplayIdentity,
        SessionId,
        SessionRecord,
        SessionState,
    )

    return SessionRecord(
        SessionId.parse(session_id),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity(slug, "codex", "regular", 4),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


@_SURFACES
async def test_a_row_names_the_project_agent_and_sequence_it_belongs_to(surface) -> None:
    """A notification said only *when* and *what*, never *whose*.

    On a host running several agents at once -- which is the host this feature exists for --
    "the agent is waiting for an answer" identifies nothing. The row now leads with the
    session's identity, resolved through the same catalogue join Stage 1 promoted.
    """
    session = "01234567-89ab-cdef-0123-456789abcdef"

    async def feed():
        return (_named_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail="May I push?"),)

    context, _sessions = _index_context(feed, (_session_record(session),))
    app = surface(context)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = _feed_rows(app)[0]
        assert "existing" in row, row
        assert "codex" in row, row
        assert "#4" in row, row
        assert "waiting for an answer" in row, row
        assert "opaque-existing" not in row, "the row must name the project, not its hash"


@_SURFACES
async def test_a_long_detail_is_ellipsised_rather_than_wrapped(surface) -> None:
    """Wrapping is what the Static did, and it is why six lines of one notification could push
    every other observation off the pane. One observation is one row."""
    session = "01234567-89ab-cdef-0123-456789abcdef"

    async def feed():
        return (
            _named_activity(
                ActivityKind.COMPLETED, minutes_ago=1, detail="x" * 400, session=session
            ),
        )

    context, _sessions = _index_context(feed, (_session_record(session),))
    app = surface(context)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = _feed_rows(app)
        assert len(rows) == 1, "one observation must occupy exactly one row"
        assert "\n" not in rows[0], "the row must not wrap"
        assert rows[0].endswith("…"), rows[0][-40:]
        assert len(rows[0]) < 400


@_SURFACES
async def test_an_observation_whose_session_is_unknown_falls_back_to_its_id(surface) -> None:
    """A notification outlives its session -- that is the point of a durable feed. A row whose
    session has been reconciled away must still say which one it was, so the bare id is the
    fallback rather than an empty identity."""

    async def feed():
        return (
            _named_activity(
                ActivityKind.QUIET, minutes_ago=2, session="deadbeef-0000-0000-0000-000000000000"
            ),
        )

    context, _sessions = _index_context(feed, ())
    app = surface(context)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = _feed_rows(app)[0]
        assert "deadbeef" in row, row
        assert "·  ·" not in row, "an unknown session must not render an empty identity"


@_SURFACES
async def test_a_notification_for_an_ended_session_still_names_its_project(surface) -> None:
    """The read is `list_sessions`, raw -- not `listed_sessions`, which filters ENDED.

    A notification outlives its session by design, so the record the feed needs to name a row
    is routinely one the *sessions list* has correctly stopped showing. Filtering here would
    make the feed's naming fail on precisely the observations most worth reading: the ones
    about work that has finished.
    """
    from remote_agents.domain.models import SessionState

    session = "01234567-89ab-cdef-0123-456789abcdef"
    # `dataclasses.replace`, not `type(x)(**x.__dict__)`: SessionRecord is frozen *and*
    # slotted, so it has no `__dict__` at all and the latter raises AttributeError.
    from dataclasses import replace as _dc_replace

    ended = _dc_replace(_session_record(session), state=SessionState.ENDED)

    async def feed():
        return (_named_activity(ActivityKind.COMPLETED, minutes_ago=3, session=session),)

    context, _sessions = _index_context(feed, (ended,))
    app = surface(context)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "existing" in _feed_rows(app)[0], _feed_rows(app)[0]


@_SURFACES
async def test_a_store_read_that_raises_leaves_the_drawn_rows_alone(surface) -> None:
    """Same contract the activity read already has: rows already on screen are stale, not
    wrong, and a background read having a bad moment must never break the position."""
    session = "01234567-89ab-cdef-0123-456789abcdef"
    failing = [False]

    class _Sessions:
        refreshed = 0

        async def refresh_readiness(self):
            return ()

        async def list_sessions(self):
            if failing[0]:
                raise RuntimeError("store contended")
            return (_session_record(session),)

    async def feed():
        return (_named_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, session=session),)

    from dataclasses import replace as _replace

    base = _context(feed)
    app = surface(_replace(base, backend=_replace(base.backend, sessions=_Sessions())))
    async with app.run_test() as pilot:
        await pilot.pause()
        before = _feed_rows(app)
        assert "existing" in before[0]

        failing[0] = True
        await app.screen._reload_feed()
        await pilot.pause()

        assert app.is_running, "a failed name read must not take the surface down"
        assert _feed_rows(app), "the pane must not be emptied by a failed name read"


@_SURFACES
async def test_the_feed_read_never_refreshes_readiness(surface) -> None:
    """A feed re-render must not start a tmux conversation. This pane repaints every ten
    seconds on two surfaces, and `refresh_readiness` rescans every record and runs a capture
    per FAILED session -- so naming the rows through the readiness pass would put a periodic
    tmux workload behind a pane whose whole job is to be glanced at."""
    session = "01234567-89ab-cdef-0123-456789abcdef"

    async def feed():
        return (_named_activity(ActivityKind.QUIET, minutes_ago=1, session=session),)

    context, sessions = _index_context(feed, (_session_record(session),))
    app = surface(context)
    async with app.run_test() as pilot:
        await pilot.pause()
        baseline = type(sessions).refreshed
        await app.screen._reload_feed()
        await pilot.pause()
        assert type(sessions).refreshed == baseline, "the feed's own read refreshed readiness"
