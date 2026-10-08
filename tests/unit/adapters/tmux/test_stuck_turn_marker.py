"""A turn marker another agent left behind neither holds an idle pane busy nor outlives a send.

On 2026-10-08 a live test's Claude, started from inside a managed pane, wrote a marker for that
pane's session under its own ownership and never ended it. The pane's own Stops could not end
it -- a hook's end only removes a marker its owner started, which is the rule that keeps a
nested agent from ending its parent's turn (DEC-104) -- so the pane read as busy for good and a
rollover against it failed `predecessor-not-idle`.

The fix is not an expiry on that rule: a parent's long turn would then hand its marker to a
nested agent, the bug the rule exists to prevent. It is that the screen decides. Once Claude's
status band no longer hides its `✻ … · done` footer, an idle screen reads idle whatever marker
stands, and the send path's own end -- unconditional, on a screen showing the turn over -- removes
the stale marker for good. These drive the real terminal and the real marker files.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.agents.turn_markers import FileTurnMarkers
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import TmuxTerminal

from .test_send_prompt import _DRAFTED, _WAITS, PromptPane, _screen

_FOREIGN_OWNER = "3eb63a06-5c77-4056-a2b6-41e98620ef85"


def _terminal(pane: PromptPane, markers: FileTurnMarkers) -> TmuxTerminal:
    composers = {
        str(descriptor.profile_id): descriptor
        for descriptor in provider_descriptors()
        if descriptor.composer is not None
    }
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-stuck-marker", pane),
        {},
        {},
        startup_timeout=1.0,
        composers=composers,
        waits=_WAITS,
        turn_markers=markers,
    )


def _stuck(tmp_path: Path, pane: PromptPane) -> FileTurnMarkers:
    """The session's marker, started by another agent that will never end it."""
    markers = FileTurnMarkers(tmp_path)
    markers.start(str(pane.session_id), owner=_FOREIGN_OWNER)
    markers.end_if_owned_by(str(pane.session_id), "b437d9e3-4f0a-48ec-8979-ae5f953832cb")
    assert markers.started_at(str(pane.session_id)) is not None, "the pane's own end refused"
    # Past the grace a fresh marker gets: the stuck one was minutes old by the time the pane sat
    # idle, its age measured from the owner's last submit.
    stale = time.time() - 600
    os.utime(tmp_path / "turns" / str(pane.session_id), (stale, stale))
    return markers


def test_an_idle_pane_under_the_plan_band_reads_idle_despite_a_stuck_marker(tmp_path) -> None:
    pane = PromptPane([_screen("claude", "idle_under_plan_band")], title="✳ Project backlog")
    markers = _stuck(tmp_path, pane)

    assert asyncio.run(_terminal(pane, markers).pane_idle(pane.session_id)) is True


def test_a_stuck_marker_on_a_busy_screen_still_reads_busy(tmp_path) -> None:
    """The screen deciding is not a bypass: a turn still running stays BUSY."""
    pane = PromptPane([_screen("claude", "busy")], title="◑ Project backlog")
    markers = _stuck(tmp_path, pane)

    assert asyncio.run(_terminal(pane, markers).pane_idle(pane.session_id)) is False


def test_a_send_into_the_idle_pane_ends_the_stuck_marker_for_good(tmp_path) -> None:
    pane = PromptPane(
        [
            _screen("claude", "idle_under_plan_band"),
            _screen("claude", "composed"),
            _screen("claude", "busy"),
        ],
        title="✳ Project backlog",
    )
    markers = _stuck(tmp_path, pane)

    asyncio.run(_terminal(pane, markers).send_prompt(pane.session_id, _DRAFTED))

    assert markers.started_at(str(pane.session_id)) is None
