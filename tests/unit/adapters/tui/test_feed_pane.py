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
travels through a `markup=False` OptionList, the same inertness rule every row sink in this
surface follows (DEC-014's spirit: text this app did not author is displayed, never
interpreted).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backends import SessionUseCaseDouble, backend_for
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


class _Launcher(SessionUseCaseDouble):
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

    class _Sessions(SessionUseCaseDouble):
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
        pane = _feed_pane(app)
        rows = _feed_rows(app)
        assert len(rows) == 1, "one observation must occupy exactly one option"
        assert rows[0].endswith("…"), rows[0][-40:]
        assert len(rows[0]) < 400

        # Asserted on the widget's own geometry, which is the direct form of "one
        # observation is one row" and is the same statement on both surfaces.
        #
        # Three earlier versions of this assertion were wrong, each in a way worth recording
        # because each looked right: `"\n" not in rows[0]` checks a prompt string that never
        # holds a newline whatever the widget does with it (this is the one that let the wrap
        # ship); filtering rendered lines by an age token also matches the dashboard's
        # *session* row; and filtering by the detail's own text fails because the detail is
        # cut off entirely in a pane a third of a column wide. The virtual height is none of
        # those -- it is the number of lines the list actually occupies.
        assert pane.virtual_size.height == pane.option_count, (
            f"{pane.option_count} option(s) occupied {pane.virtual_size.height} lines"
        )


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

    class _Sessions(SessionUseCaseDouble):
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


async def test_the_console_feed_pane_holds_the_keyboard_on_arrival() -> None:
    """The console's feed pane is nothing but this list, so the list is what the keys are for.

    Found by the stage's live check, not by a unit test: every case above drives `_reload_feed`
    directly or sets `highlighted` itself, so all of them passed against a pane the arrow keys
    could not reach. On arrival focus sat on `#filter` -- a `display: none` Input this screen
    composes only because `ChoiceScreen`'s machinery queries it by id -- and Down did nothing
    until the owner pressed Tab first. A pane whose whole content is a scrollable list, whose
    scrolling is one undiscoverable keystroke away, is the defect this stage set out to remove
    wearing a different hat.

    The dashboard deliberately does *not* do this: there the filter legitimately owns the
    keyboard for typing a project filter, and the feed is three Tabs away by design.
    """

    async def feed():
        return tuple(
            _activity(ActivityKind.COMPLETED, minutes_ago=minute, detail=f"row {minute}")
            for minute in range(30)
        )

    app = FeedPane(_context(feed))
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        assert pane.has_focus, f"the feed pane did not take the keyboard; {app.focused!r} did"

        await pilot.press("down")
        await pilot.pause()
        assert pane.highlighted == 1, "Down must move the cursor without a Tab first"


async def test_the_dashboard_leaves_the_keyboard_in_its_filter() -> None:
    """The other half of the rule above, so a later change cannot quietly take the filter's
    keyboard away in the name of making the feed focusable."""
    from textual.widgets import Input

    async def feed():
        return (_activity(ActivityKind.COMPLETED, minutes_ago=1),)

    app = RemoteAgentsTui(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.focused, Input), (
            f"the dashboard filter lost the keyboard to {app.focused!r}"
        )


# The pane's "never an exception" contract, at the draw as well as the read -------------------


@_SURFACES
async def test_two_observations_sharing_a_timestamp_do_not_take_the_app_down(surface) -> None:
    """`feed_key` composes session, kind and `observed_at`, and nothing makes that unique.

    `agent_activity`'s only unique column is `activity_id`, which `activity_store` discards
    before an `AgentActivity` is built -- so two rows with the same session, kind and
    microsecond are representable, and this project already treats that collision as a real
    event rather than a theoretical one (`activity_spool._MAXIMUM_NAME_ATTEMPTS` exists for
    exactly it). `OptionList.add_options` raises `DuplicateID` on a repeated id, and the draw
    runs inside a `Timer` callback, where `App._handle_exception` "Always results in the app
    exiting" -- and the offending pair stays in the newest FEED_LIMIT rows, so it would take
    the surface down again on every tick until new activity pushed it out.

    The same failure class `SessionsScreen._draw_listing`'s docstring records having already
    been fixed once, in a new code path.
    """
    stamp = datetime.now(UTC)
    twin = AgentActivity(
        "01234567-89ab-cdef-0123-456789abcdef",
        ActivityKind.COMPLETED,
        "first",
        stamp,
        ActivityConfidence.REPORTED,
    )
    other = AgentActivity(
        "01234567-89ab-cdef-0123-456789abcdef",
        ActivityKind.COMPLETED,
        "second",
        stamp,
        ActivityConfidence.REPORTED,
    )

    async def feed():
        return (twin, other)

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running, "a duplicate row id took the surface down"
        pane = _feed_pane(app)
        assert pane.option_count == 2, "both observations must still be drawn"
        ids = [pane.get_option_at_index(i).id for i in range(pane.option_count)]
        assert len(set(ids)) == 2, ids


@_SURFACES
async def test_a_failure_building_the_rows_leaves_the_pane_as_it_was(surface) -> None:
    """The docstring promises "never an exception" for the whole method, not for the read.

    The activity read was guarded and everything after it was not, so any failure in the
    naming join or the option rebuild propagated out of a Timer callback and exited the app.
    """
    from remote_agents.adapters.tui.screens import feed as feed_module

    async def feed():
        return (_activity(ActivityKind.COMPLETED, minutes_ago=1, detail="drawn"),)

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        before = _feed_rows(app)
        assert before

        original = feed_module.feed_rows
        feed_module.feed_rows = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            await app.screen._reload_feed()
            await pilot.pause()
            assert app.is_running, "a failed row build took the surface down"
            assert _feed_rows(app) == before, "the drawn rows must be left alone"
        finally:
            feed_module.feed_rows = original


async def test_the_narrow_dashboard_region_still_names_project_agent_and_sequence() -> None:
    """The goal names three things a row must carry, and the dashboard is where they are
    hardest to fit: its feed region is `2fr` of a `3fr/2fr` split, so at the project's own
    100-column baseline it has roughly 36 usable columns for an identity that is 32.

    Leading with the age spent 11 of those on a timestamp and cut the row mid-identity --
    `0m ago · existing · claude · regula…` -- losing the sequence and the kind words both.
    The list is already ordered newest-first, so position carries recency and the identity
    is what the row is *for*. Age moves after it and falls off the narrow pane instead.
    """
    session = "01234567-89ab-cdef-0123-456789abcdef"

    async def feed():
        return (_named_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, session=session),)

    context, _sessions = _index_context(feed, (_session_record(session),))
    app = RemoteAgentsTui(context)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        # Measured, not scraped. The dashboard's *sessions* row carries a byte-identical
        # identity prefix, so no filter over the rendered screen can reliably tell the two
        # apart -- an earlier version of this test matched the sessions row and passed
        # against a feed row that was in fact cut mid-identity. Truncating the real prompt to
        # the real measured pane width asks the question directly: does the content the goal
        # requires survive at the width this surface actually gives the feed?
        prompt = str(pane.get_option_at_index(0).prompt)
        visible = prompt[: pane.content_size.width]
        assert "existing" in visible, visible
        assert "codex" in visible, visible
        assert "#4" in visible, (
            f"the sequence does not fit {pane.content_size.width} columns: {visible!r}"
        )
        assert pane.virtual_size.height == pane.option_count


# Expansion state: at most one row open, and per instance --------------------------------------


@_SURFACES
async def test_opening_a_second_row_closes_the_first(surface) -> None:
    """The pane is a third of a column on the dashboard. Two rows open at once leaves nothing
    left to scan, which is the opposite of what a glanceable feed is for."""

    observations = (
        _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail="first detail"),
        _activity(ActivityKind.COMPLETED, minutes_ago=5, detail="second detail"),
    )

    async def feed():
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        first, second = (pane.get_option_at_index(i).id for i in range(2))

        await app.screen.choose(first)
        await pilot.pause()
        assert app.screen.opened_notification == first

        await app.screen.choose(second)
        await pilot.pause()
        assert app.screen.opened_notification == second, "opening a second row must close the first"


@_SURFACES
async def test_enter_on_an_open_row_closes_it(surface) -> None:
    observations = (_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail="the detail"),)

    async def feed():
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _feed_pane(app).get_option_at_index(0).id

        await app.screen.choose(key)
        await pilot.pause()
        assert app.screen.opened_notification == key

        await app.screen.choose(key)
        await pilot.pause()
        assert app.screen.opened_notification is None, "Enter on an open row must close it"


async def test_the_expansion_state_is_per_instance_not_shared() -> None:
    """`FeedRegion` is a mixin over two screens that can both exist at once -- the dashboard in
    one process, the console's feed pane in another. A class-level default that got *assigned*
    would be per instance; one that got mutated in place would not, which is the trap
    `_feed_news` already documents having stepped around."""

    observations = (_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail="d"),)

    async def feed():
        return observations

    dashboard = RemoteAgentsTui(_context(feed))
    pane_app = FeedPane(_context(feed))
    async with dashboard.run_test() as dash_pilot:
        await dash_pilot.pause()
        key = _feed_pane(dashboard).get_option_at_index(0).id
        await dashboard.screen.choose(key)
        await dash_pilot.pause()
        assert dashboard.screen.opened_notification == key

        async with pane_app.run_test() as pane_pilot:
            await pane_pilot.pause()
            assert pane_app.screen.opened_notification is None, (
                "one surface's open row leaked into the other"
            )


# The expansion itself -------------------------------------------------------------------------

_LONG_DETAIL = (
    "May I push the branch and open a pull request against main? "
    "The rebase is clean and the suite is green, but the changelog entry is still missing."
)


@_SURFACES
async def test_enter_emits_the_full_detail_as_continuation_rows(surface) -> None:
    """DEC-037's whole argument: the feed exists to save the owner a trip to the session, so
    the words must stay reachable. Stage 2 bounded what is *drawn* -- this is where the rest
    comes back, one keypress away rather than gone."""

    observations = (_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail=_LONG_DETAIL),)

    async def feed():
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _feed_pane(app).get_option_at_index(0).id
        assert _feed_pane(app).option_count == 1

        await app.screen.choose(key)
        await pilot.pause()

        pane = _feed_pane(app)
        assert pane.option_count > 1, "Enter emitted no continuation rows"
        # Whitespace collapsed: the continuation rows are indented by two and wrapped at the
        # pane width, so joining them reproduces the sentence with the wrap points still in it.
        # The assertion is about the words surviving, not about where the lines broke.
        expanded = " ".join(
            " ".join(str(pane.get_option_at_index(i).prompt).split())
            for i in range(1, pane.option_count)
        )
        # The *full* detail, not the truncated form the collapsed row carries.
        assert "changelog entry is still missing" in expanded, expanded
        assert "…" not in expanded, "the expansion must not itself be elided"


@_SURFACES
async def test_every_continuation_row_is_disabled(surface) -> None:
    """The cursor must not be able to come to rest on a fragment of text and answer Enter
    with nothing. Disabled options render and scroll but cannot be selected."""

    observations = (_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail=_LONG_DETAIL),)

    async def feed():
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _feed_pane(app).get_option_at_index(0).id
        await app.screen.choose(key)
        await pilot.pause()

        pane = _feed_pane(app)
        for index in range(1, pane.option_count):
            option = pane.get_option_at_index(index)
            assert option.disabled is True, f"row {index} is selectable: {option.prompt!r}"


@_SURFACES
async def test_closing_removes_the_continuation_rows(surface) -> None:
    observations = (_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail=_LONG_DETAIL),)

    async def feed():
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _feed_pane(app).get_option_at_index(0).id
        await app.screen.choose(key)
        await pilot.pause()
        assert _feed_pane(app).option_count > 1

        await app.screen.choose(key)
        await pilot.pause()
        assert _feed_pane(app).option_count == 1, "closing left orphaned continuation rows"


@_SURFACES
async def test_a_row_with_no_detail_toggles_without_emitting_an_empty_row(surface) -> None:
    """QUIET carries no detail. Its row still answers Enter -- refusing would make the key
    mean different things on different rows -- but it has nothing to show, and an empty
    continuation row would be a blank line the cursor skips over for no reason."""

    observations = (_activity(ActivityKind.QUIET, minutes_ago=1, detail=None),)

    async def feed():
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _feed_pane(app).get_option_at_index(0).id

        await app.screen.choose(key)
        await pilot.pause()
        assert app.screen.opened_notification == key, "the row must still toggle"
        assert _feed_pane(app).option_count == 1, "an empty detail emitted a continuation row"


# An open row survives the ten-second repaint ---------------------------------------------------


@_SURFACES
async def test_a_reload_that_adds_a_newer_observation_keeps_the_open_row_open(surface) -> None:
    """The pane repaints on a 10s interval and on every reveal. An expansion discarded under
    the owner's cursor reads as the surface refusing the key they just pressed -- and the
    window is widest exactly when the feed is busy, which is when they are reading it."""
    rows = [
        (
            _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail=_LONG_DETAIL),
            _activity(ActivityKind.COMPLETED, minutes_ago=5, detail="older"),
        )
    ]

    async def feed():
        return rows[0]

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _feed_pane(app).get_option_at_index(0).id
        await app.screen.choose(key)
        await pilot.pause()
        expanded_before = _feed_pane(app).option_count
        assert expanded_before > 2

        # A newer observation arrives at the head, pushing the open row down one.
        rows[0] = (_activity(ActivityKind.LIMIT_REACHED, minutes_ago=0, detail="new"), *rows[0])
        await app.screen._reload_feed()
        await pilot.pause()

        pane = _feed_pane(app)
        assert app.screen.opened_notification == key, "the reload closed the open row"
        ids = [pane.get_option_at_index(i).id for i in range(pane.option_count)]
        assert key in ids, "the open row itself vanished"
        assert any(i.startswith(f"{key}:detail:") for i in ids), (
            "the open row is still open but drew no continuation rows"
        )
        # It moved down by exactly the one observation that arrived above it.
        assert ids.index(key) == 1


@_SURFACES
async def test_an_open_row_that_ages_out_collapses_cleanly(surface) -> None:
    """`FEED_LIMIT` bounds the pane, so an open row can be pushed out of the window entirely.
    Its continuation rows must go with it rather than being left behind attached to nothing."""
    from remote_agents.adapters.tui.context import FEED_LIMIT

    opened = _activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail=_LONG_DETAIL)
    rows = [(opened,)]

    async def feed():
        return rows[0]

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _feed_pane(app).get_option_at_index(0).id
        await app.screen.choose(key)
        await pilot.pause()
        assert _feed_pane(app).option_count > 1

        # Enough newer observations to push the open one past FEED_LIMIT.
        rows[0] = (
            *(
                _activity(ActivityKind.COMPLETED, minutes_ago=0, detail=f"newer {n}")
                for n in range(FEED_LIMIT + 2)
            ),
            opened,
        )
        await app.screen._reload_feed()
        await pilot.pause()

        pane = _feed_pane(app)
        ids = [pane.get_option_at_index(i).id for i in range(pane.option_count)]
        assert key not in ids, "the aged-out row is still drawn"
        assert not any(":detail:" in i for i in ids), "orphaned continuation rows survived"
        assert pane.option_count == FEED_LIMIT


@_SURFACES
async def test_an_over_long_detail_cannot_flood_the_pane(surface) -> None:
    """`_continuation_rows` must bound itself rather than trust a cap set in another module.

    `ports/agent_activity.MAXIMUM_DETAIL_CHARACTERS` is 240 and `bounded_detail_line` applies
    it -- but `adapters/sqlite/activity_store` reconstructs `AgentActivity` straight from the
    database, so a row written by an older or a foreign writer never passes through that
    function at all. Without a bound here, one such row would emit hundreds of disabled
    options into the pane on every redraw, inside a `Timer` callback.
    """
    from remote_agents.adapters.tui.screens.feed import MAXIMUM_CONTINUATION_ROWS

    observations = (_activity(ActivityKind.COMPLETED, minutes_ago=1, detail="word " * 4000),)

    async def feed():
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        key = _feed_pane(app).get_option_at_index(0).id
        await app.screen.choose(key)
        await pilot.pause()

        pane = _feed_pane(app)
        continuations = pane.option_count - 1
        assert continuations <= MAXIMUM_CONTINUATION_ROWS, (
            f"one detail emitted {continuations} rows"
        )
        assert continuations > 0, "the expansion showed nothing at all"


@_SURFACES
async def test_expanding_the_second_of_two_colliding_rows_keys_it_apart(surface) -> None:
    """The duplicate-timestamp path and the expansion path, together.

    Each is covered alone; this is the interaction. The second of a colliding pair carries an
    ordinal suffix, so its continuation ids must hang off *that* key rather than off the first
    row's -- otherwise opening one would expand the other, or `add_options` would raise on a
    repeated id and exit the app from a timer.
    """
    stamp = datetime.now(UTC)
    pair = tuple(
        AgentActivity(
            "01234567-89ab-cdef-0123-456789abcdef",
            ActivityKind.COMPLETED,
            f"detail number {n}",
            stamp,
            ActivityConfidence.REPORTED,
        )
        for n in range(2)
    )

    async def feed():
        return pair

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        second = pane.get_option_at_index(1).id
        assert second.endswith(":1"), second

        await app.screen.choose(second)
        await pilot.pause()

        pane = _feed_pane(app)
        ids = [pane.get_option_at_index(i).id for i in range(pane.option_count)]
        assert len(set(ids)) == len(ids), f"duplicate option ids: {ids}"
        continuations = [i for i in ids if ":detail:" in i]
        assert continuations, "the second row did not expand"
        assert all(i.startswith(f"{second}:detail:") for i in continuations), continuations
        expanded = " ".join(
            " ".join(str(pane.get_option_at_index(i).prompt).split())
            for i in range(pane.option_count)
            if ":detail:" in str(pane.get_option_at_index(i).id)
        )
        assert "detail number 1" in expanded, expanded


async def test_rows_of_different_kinds_are_distinguishable_at_the_narrow_width() -> None:
    """The dashboard's feed region is ~36 columns, and a glance has to tell rows apart.

    Measured before this was fixed: with the mode in the identity and kind words that all
    began "the agent", three observations of three different kinds truncated to the *same 36
    characters* --

        'existing · claude · regular · #1 · t'   (waiting for an answer)
        'existing · claude · regular · #1 · t'   (finished its work)
        'existing · claude · regular · #1 · t'   (gone quiet)

    An expansion is a *targeted* read: at most one row opens at a time, so a pane of
    indistinguishable rows means opening each in turn to find the one that needs an answer.
    The collapsed row has to discriminate for the keypress to be worth anything.
    """
    session = "01234567-89ab-cdef-0123-456789abcdef"
    stamp = datetime.now(UTC)
    kinds = (
        ActivityKind.NEEDS_ANSWER,
        ActivityKind.COMPLETED,
        ActivityKind.LIMIT_REACHED,
        ActivityKind.OUTPUT_LIMIT,
        ActivityKind.QUIET,
    )
    observations = tuple(
        AgentActivity(
            session, kind, None, stamp - timedelta(minutes=n), ActivityConfidence.REPORTED
        )
        for n, kind in enumerate(kinds)
    )

    async def feed():
        return observations

    context, _sessions = _index_context(feed, (_session_record(session),))
    app = RemoteAgentsTui(context)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        width = pane.content_size.width
        glances = [
            str(pane.get_option_at_index(i).prompt)[:width] for i in range(pane.option_count)
        ]
        assert len(set(glances)) == len(kinds), (
            f"{len(kinds)} kinds collapsed to {len(set(glances))} distinct rows at "
            f"{width} columns:\n" + "\n".join(repr(g) for g in glances)
        )


@_SURFACES
async def test_a_real_enter_keypress_expands_the_highlighted_row(surface) -> None:
    """Every other expansion case calls `choose` directly, which skips the keypress ->
    `OptionSelected` -> `choose` path entirely. This drives the key an owner actually presses."""
    observations = (_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail=_LONG_DETAIL),)

    async def feed():
        return observations

    app = surface(_context(feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = _feed_pane(app)
        # Focus the pane the way the owner would: the console pane already holds the keyboard,
        # the dashboard's is reached by Tab.
        while not pane.has_focus:
            await pilot.press("tab")
            await pilot.pause()
        assert pane.highlighted is not None

        await pilot.press("enter")
        await pilot.pause()
        assert _feed_pane(app).option_count > 1, "Enter did not expand the highlighted row"

        await pilot.press("enter")
        await pilot.pause()
        assert _feed_pane(app).option_count == 1, "Enter did not collapse it again"
