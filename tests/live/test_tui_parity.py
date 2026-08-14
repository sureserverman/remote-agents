"""Opt-in acceptance: the real terminal application, driven by keys against real tmux.

This is the parity claim exercised above the hand-written doubles every other tier uses. It
builds `RemoteAgentsTui` over the composition root's own `local_context`, mounts it through
Textual's `Pilot`, and then does everything through the surface itself — arrow keys and enter,
the rows the app actually renders, the modal it puts in front of a kill. No step here calls a
service method to make a screen do something, which is the whole point: what a step proves is
that the *surface* reached it, not that the composition behind it works.

The service composition beside it stands in for the one thing this surface is not — the second
writer. Every session is launched from a `SessionService` on its own connection, exactly as the
running bot would, so what the terminal lists is always a session it did not start itself.

**Three isolations, because a keyboard-driven force stop is otherwise loose in the same house
as the owner's real agents.**

- **A tmux server of this run's own.** `TMUX_TMPDIR` is pointed at the test's temporary
  directory before anything runs tmux, so the composition root's own `tmux -L remote-agents`
  resolves to a socket underneath it. Same socket name, same composition, a different server:
  the owner's panes are not reachable from this process at all.
- **A store of this run's own.** The connections are opened on a throwaway database, so the
  sessions list holds exactly what this file launched — there is no row a stray keypress could
  land on that names somebody else's agent.
- **Every session started is retired in a `finally`**, including on assertion failure.

`REMOTE_AGENTS_LIVE_ACCEPTANCE=1` is required. Without it every test here skips, because
running it unattended would create and destroy real agent panes.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

import pytest
from textual.css.query import NoMatches
from textual.widgets import OptionList, TextArea
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.codec import attach_argv
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.model import _BACK, _EMPTY
from remote_agents.application.commands import ForceStopCommand, LaunchCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import SessionService
from remote_agents.bootstrap import ProjectCatalogueProvider, _local_runtime, local_context
from remote_agents.config import load_config
from remote_agents.domain.models import ProfileId, ProjectId, SessionRecord, SessionState
from remote_agents.production import ProductionPaths

#: How long a driven step is given to land, as tries by interval rather than as one wait. The
#: bound is a ceiling on giving up, never a guess at how long this host needs (BL-017): the
#: longest step driven here is a graceful stop, which the composition polls for up to its own
#: 20-second budget, so a tighter ceiling would be a race rather than a bound.
#:
#: A try is one pump of the surface *plus* the interval, so the wall-clock ceiling is well
#: above the 12 seconds the numbers alone give — measured at roughly 48 by timing a step that
#: deliberately never lands. That is only ever the cost of a failing run.
_SETTLE_TRIES = 1200
_SETTLE_INTERVAL = 0.01


def _enabled() -> tuple[Path, ProductionPaths]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    paths = ProductionPaths.for_home(Path.home())
    if not paths.config_path.is_file():
        pytest.skip("BLOCKED: production config is unavailable")
    return paths.config_path, paths


def _key(prefix: str) -> str:
    """A fresh idempotency key per run; a date-based one refuses a same-day re-run."""
    return f"{prefix}-{date.today()}-{uuid4()}"


def _this_project(config):
    return next(
        (
            item
            for item in ProjectCatalogueProvider(config.registry_path, config.dev_root)
            .refresh()
            .catalogue
            if item.name == "remote-agents"
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class _Harness:
    """The real app, the second writer beside it, and the tmux server both of them talk to."""

    app: RemoteAgentsTui
    service: SessionService
    terminal: TmuxTerminal
    project: CatalogProject
    profile: ProfileId
    connections: tuple[Connection, ...]

    async def start(self, label: str) -> SessionRecord:
        """Launch one throwaway session from the second writer, as the running service would."""
        return await self.service.launch(
            LaunchCommand(ProjectId(self.project.opaque_id), self.profile, _key(label), label)
        )

    async def state_of(self, record: SessionRecord) -> SessionState:
        """What the store says about one session, read on the connection the app never uses."""
        return next(
            item.state
            for item in await self.service.list_sessions()
            if item.session_id == record.session_id
        )

    def close(self) -> None:
        for connection in self.connections:
            connection.close()


def _harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Compose the real surface over a tmux server and a store belonging to this run alone.

    `local_context` is the composition root's own, unmodified: DEC-001 is what forbids a
    harness from assembling a terminal by hand and then claiming it drove the real one. What
    the two substitutions below change is where that composition *lands* — `TMUX_TMPDIR` before
    any tmux call, and the connection the caller already passes it — so nothing about the
    surface, the service, or the runtime between them is stood in for.

    The provider is refreshed before its `paths` are read: the routing table is empty until
    then, and a terminal built on an empty one cannot resolve any project's directory, so every
    launch through it fails immediately.
    """
    config_path, paths = _enabled()
    config = load_config(config_path)
    project = _this_project(config)
    if project is None:
        pytest.skip("BLOCKED: this project is not in the catalogue")
    profile = ProfileId(os.environ.get("REMOTE_AGENTS_ACCEPTANCE_PROFILE", "claude"))
    # Set before anything runs tmux, and read by tmux itself rather than by this project:
    # `tmux -L remote-agents` resolves its socket under `$TMUX_TMPDIR/tmux-$UID`, so this is
    # what makes the composition root's hard-coded socket name name a server of this run's own.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))

    store = tmp_path / "sessions.sqlite3"
    surface_connection = open_database(store, migrations=MIGRATIONS)
    writer_connection = open_database(store, migrations=MIGRATIONS)
    context = local_context(config, surface_connection, paths)
    if str(profile) not in {choice.profile_id for choice in context.profiles if choice.available}:
        surface_connection.close()
        writer_connection.close()
        pytest.skip(f"BLOCKED: {profile} is not available on this host")
    provider = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    provider.refresh()
    runtime = _local_runtime(config, paths, provider.paths)
    return _Harness(
        RemoteAgentsTui(context),
        SessionService(SQLiteSessionStore(writer_connection), runtime.terminal),
        runtime.terminal,
        project,
        profile,
        (surface_connection, writer_connection),
    )


def _rows(app: RemoteAgentsTui) -> list[str | None]:
    """The row keys the position on screen is offering, or none while it is still composing.

    `NoMatches` is answered rather than raised because this is read from inside `_until`, which
    pumps across screen pushes: a position whose widgets are half-mounted has no rows yet, which
    is the same answer as a position that has not drawn them, and both mean "keep waiting".
    """
    try:
        choices = app.screen.query_one("#choices", OptionList)
    except NoMatches:
        return []
    return [option.id for option in choices.options]


async def _until(pilot, settled, description: str) -> None:
    """Pump the surface until it answers `settled`, then return — or fail naming what did not.

    The counterpart to `settle()` in `test_tui_snapshots.py` and `settle_ready` in
    `tests/e2e/test_terminal_launch.py`, and it is here for the reason both of those give: every
    step driven in this file crosses a real tmux server and a real store, so how long one takes
    is a property of the host rather than something a test may encode as a pause. The sleep is
    the polling interval; the condition is what is waited on.
    """
    for _ in range(_SETTLE_TRIES):
        await pilot.pause()
        if settled():
            return
        await asyncio.sleep(_SETTLE_INTERVAL)
    raise AssertionError(f"the surface never {description}")


async def _choose(app: RemoteAgentsTui, pilot, key: str) -> None:
    """Walk the cursor onto one row with the arrow keys and press enter, as the owner does.

    The walk is bounded by the row count and **the landing is asserted before the enter**. That
    assertion is the safety rule of this file expressed as code rather than as a comment: this
    is the only way a destructive row is reached here, so a keypress that came to rest anywhere
    else has to fail the test rather than issue whatever command it happens to be sitting on.
    """
    choices = app.screen.query_one("#choices", OptionList)
    keys = _rows(app)
    assert key in keys, f"{position(app)} offers {keys}, not {key!r}"
    for _ in range(len(keys)):
        if choices.highlighted == keys.index(key):
            break
        await pilot.press("down")
    resting = choices.highlighted
    landed = keys[resting] if resting is not None else None
    assert landed == key, f"the cursor came to rest on {landed!r} rather than on {key!r}"
    await pilot.press("enter")


@pytest.mark.live_acceptance
async def test_the_terminal_manages_a_session_the_service_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """List, detail, copy attach, inspect and graceful stop — every one of them by keypress."""
    harness = _harness(tmp_path, monkeypatch)
    app = harness.app
    record = None
    try:
        record = await harness.start("tui-parity")
        assert record.state is SessionState.RUNNING

        async with app.run_test() as pilot:
            # 1. The terminal sees a session it did not start.
            await pilot.press("ctrl+s")
            await _until(
                pilot,
                lambda: position(app) == "SESSIONS" and str(record.session_id) in _rows(app),
                "listed the session the service started",
            )

            # 2. Detail: opened from the row, and about the session that row named.
            await _choose(app, pilot, str(record.session_id))
            await _until(
                pilot,
                lambda: position(app) == "SESSION_DETAIL" and _rows(app),
                "opened the session's detail",
            )
            assert app.screen.session_value == str(record.session_id)
            assert record.state.value in _status(app), _status(app)

            # 3. Copy attach, byte for byte what the owner would paste.
            await _choose(app, pilot, "attach")
            await _until(
                pilot,
                lambda: _status(app).startswith("Attach with: "),
                "rendered the attach command",
            )
            assert _status(app) == f"Attach with: {' '.join(attach_argv(record.session_id))}"

            # 4. Inspect: a real capture of a real pane, on the screen that shows it.
            await _choose(app, pilot, "inspect")
            await _until(pilot, lambda: position(app) == "INSPECT", "opened the captured output")
            assert app.screen.query_one("#output", TextArea).text
            await pilot.press("escape")
            await _until(
                pilot, lambda: position(app) == "SESSION_DETAIL", "came back to the detail"
            )

            # 5. Graceful stop, chosen from the detail, retires the session — and the redraw
            # that follows it is the observable outcome: the record is ENDED, ENDED is filtered
            # from what any surface can act on, so the detail has nothing left to offer.
            await _choose(app, pilot, "graceful")
            await _until(
                pilot,
                lambda: _status(app) == "That session is no longer available.",
                "reported the stopped session as gone",
            )
            assert _rows(app) == [_BACK]

        assert await harness.state_of(record) is SessionState.ENDED
        assert await harness.terminal.inspect(record.session_id) is None
        record = None
    finally:
        await _retire(harness.service, record)
        harness.close()


@pytest.mark.live_acceptance
async def test_the_terminal_force_stops_a_session_through_the_confirmation_modal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force is destructive, so it is proved on a session this test started, and only that one.

    The confirmation is *presented and answered*, rather than stepped around by issuing the
    command: the modal is what stands between a rendered row and a killed agent, and a live
    regression in it is invisible to any check that reaches past it.
    """
    harness = _harness(tmp_path, monkeypatch)
    app = harness.app
    record = None
    try:
        record = await harness.start("tui-force")
        assert record.state is SessionState.RUNNING
        live = await harness.terminal.inspect(record.session_id)
        assert live is not None and live.live, "there was no pane for the force stop to kill"

        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await _until(
                pilot,
                lambda: position(app) == "SESSIONS" and str(record.session_id) in _rows(app),
                "listed the session to be force stopped",
            )
            await _choose(app, pilot, str(record.session_id))
            await _until(
                pilot,
                lambda: position(app) == "SESSION_DETAIL" and "force" in _rows(app),
                "offered Force stop on the detail",
            )

            await _choose(app, pilot, "force")
            await _until(pilot, lambda: position(app) == "FORCE_MODAL", "opened the confirmation")
            assert app.screen.is_modal, "an app binding could walk away from an unanswered kill"
            assert record.display.rendered in _status(app), "the confirmation must name the session"
            resting = app.screen.query_one("#choices", OptionList).highlighted
            assert resting is not None, "the owner cannot see which row an enter would activate"
            assert _rows(app)[resting] != "force-confirm", "a stray enter would have killed it"

            # The second deliberate act, and the only thing in this file that destroys anything.
            await _choose(app, pilot, "force-confirm")
            await _until(
                pilot,
                lambda: (
                    position(app) == "SESSION_DETAIL"
                    and _status(app) == "That session is no longer available."
                ),
                "returned to a detail with nothing left to act on",
            )

        assert await harness.state_of(record) is SessionState.ENDED, "the record survived the kill"
        assert await harness.terminal.inspect(record.session_id) is None, "the pane is still there"
        record = None
    finally:
        await _retire(harness.service, record)
        harness.close()


@pytest.mark.live_acceptance
async def test_the_terminal_offers_only_resume_capable_agents_without_resuming_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume's read half is safe to drive; starting a real resumed agent is not.

    Resuming would create a second live pane against a real conversation. What the surface can
    be held to unattended is which agents it *offers*: DEC-002 says that comes from a live
    capability probe rather than from a table, so the rows this flow renders must be exactly the
    profiles this host reports as resume-capable — and when none is, the screen has to say so
    rather than show an empty rectangle. The resume itself stays an owner step, recorded in the
    acceptance document.
    """
    harness = _harness(tmp_path, monkeypatch)
    app = harness.app
    try:
        conversations = app.services.conversations
        assert conversations is not None, "the terminal composition wired no conversations"
        capabilities = await conversations.capabilities()
        assert capabilities, "no profile reported a resume capability on this host"
        # Every capability is truthful about itself rather than assumed available.
        for capability in capabilities:
            if not capability.catalogue_available:
                assert capability.reason, f"{capability.profile_id} is unavailable with no reason"
        capable = {
            str(capability.profile_id)
            for capability in capabilities
            if capability.catalogue_available and capability.selected_resume_available
        }

        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            await _until(
                pilot,
                lambda: (
                    position(app) == "RESUME_PROJECTS" and harness.project.opaque_id in _rows(app)
                ),
                "opened the resume flow on the real catalogue",
            )
            await _choose(app, pilot, harness.project.opaque_id)
            await _until(
                pilot,
                lambda: position(app) == "RESUME_PROFILES" and _rows(app),
                "asked this host which agents can resume",
            )
            rows = _rows(app)

        assert {key for key in rows if key not in {_BACK, _EMPTY}} == capable, (
            "the flow offers agents this host cannot resume with"
        )
        if not capable:
            assert _EMPTY in rows, "an unresumable host must say so rather than render nothing"
    finally:
        harness.close()


async def _retire(service: SessionService, record: SessionRecord | None) -> None:
    """Leave nothing running, whatever failed above."""
    if record is None:
        return
    try:
        await service.force_stop(ForceStopCommand(record.session_id))
    except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
        pass
