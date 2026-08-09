"""Copy attach is the recovery path; when it is unavailable the owner is told why."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from textual.widgets import OptionList
from tui_feedback import announcements
from tui_feedback import status as _status

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_ARGV = ("tmux", "-L", "remote-agents", "attach-session", "-t")


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Listing:
    records: tuple[SessionRecord, ...] = ()
    attach: str | None = None
    asked: list[SessionId] = field(default_factory=list)

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, session_id: SessionId) -> str | None:
        self.asked.append(session_id)
        return self.attach


def _context(launcher: _Listing) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_EXISTING,),
        attach_argv=lambda session_id: (*_ARGV, f"={session_id}"),
        catalogue=(_EXISTING,),
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


async def test_the_attach_command_is_rendered_byte_for_byte() -> None:
    record = _record()
    expected = " ".join((*_ARGV, f"={record.session_id}"))
    launcher = _Listing((record,), attach=expected)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("attach")
        await pilot.pause()
        status = _status(app)

    assert expected in status


async def test_the_command_is_rendered_verbatim_and_not_reformatted() -> None:
    """The owner pastes this string, so the surface must not normalize it.

    Deliberately awkward spacing: an implementation that split and rejoined, stripped, or
    re-derived the command from its own `attach_argv` would silently "tidy" it and this
    assertion would fail. That is the real invariant — the previous version of this test
    compared two strings the test itself had made equal, and could not have caught it.
    """
    record = _record()
    awkward = "tmux  -L remote-agents   attach-session -t  =weird-spacing"
    launcher = _Listing((record,), attach=awkward)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("attach")
        await pilot.pause()
        status = _status(app)

    assert awkward in status


async def test_an_unavailable_attach_says_why_instead_of_hiding() -> None:
    """The bot hides the affordance silently; the local surface must not repeat that."""
    record = _record()
    launcher = _Listing((record,), attach=None)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("attach")
        await pilot.pause()
        reported = " ".join(announcements(app)).casefold()

    assert "not available" in reported or "unavailable" in reported
    assert "pane" in reported
    assert launcher.asked == [record.session_id]


async def test_the_attach_entry_is_offered_on_a_live_session() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,), attach="tmux attach")))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        rows = _rows(app)

    assert any("attach" in row.casefold() for row in rows)


async def test_the_affordance_is_present_even_when_the_pane_is_dead() -> None:
    """Presence is what lets the owner learn *why*; hiding it teaches nothing."""
    record = _record(SessionState.PRESERVED)
    app = RemoteAgentsTui(_context(_Listing((record,), attach=None)))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        rows = _rows(app)

    assert any("attach" in row.casefold() for row in rows)


async def test_the_command_actually_reaches_the_clipboard() -> None:
    """The affordance has been called "Copy attach" since it was written and only printed.

    `App.copy_to_clipboard` writes OSC 52, which is what makes the name true — and works
    through SSH and inside tmux, which is where this surface is used.
    """
    record = _record()
    launcher = _Listing((record,), attach="tmux -L remote-agents attach-session -t =abc")
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("attach")
        await pilot.pause()
        clipboard = app._clipboard
        status = _status(app)

    assert clipboard == "tmux -L remote-agents attach-session -t =abc"
    assert clipboard in status, (
        "the printed fallback was dropped; OSC 52 is the half that can silently fail"
    )


async def test_the_clipboard_message_does_not_claim_an_outcome_it_cannot_observe() -> None:
    """A terminal that ignores OSC 52 reports nothing back, so "Copied." would be a guess.

    The same rule sub-plan 3 applied to stop results, one surface over: say what was
    attempted, not what the other end did with it.
    """
    record = _record()
    launcher = _Listing((record,), attach="tmux attach -t =abc")
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("attach")
        await pilot.pause()
        said = " ".join(announcements(app))

    assert "clipboard" in said.casefold()
    assert "on screen too" in said, said


async def test_an_unavailable_attach_copies_nothing() -> None:
    """A refusal must not leave the previous session's command on the clipboard."""
    record = _record()
    launcher = _Listing((record,), attach=None)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("attach")
        await pilot.pause()
        clipboard = app._clipboard

    assert clipboard == "", f"a failed attach wrote {clipboard!r} to the clipboard"
