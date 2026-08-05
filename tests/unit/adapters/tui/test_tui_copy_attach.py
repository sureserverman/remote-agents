"""Copy attach is the recovery path; when it is unavailable the owner is told why."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

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


def _status(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#status").content)


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(item.query_one("Label").content) for item in app.query("ListView > ListItem")]


async def test_the_attach_command_is_rendered_byte_for_byte() -> None:
    record = _record()
    expected = " ".join((*_ARGV, f"={record.session_id}"))
    launcher = _Listing((record,), attach=expected)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("attach")
        await pilot.pause()
        status = _status(app)

    assert expected in status


async def test_the_rendered_command_equals_attach_argv_joined() -> None:
    """The surface must not reformat the command it tells the owner to paste.

    Both sources are the same function in production — `attach_command()` is
    `" ".join(attach_argv(...))` in `adapters/tmux/codec.py`, and bootstrap hands that same
    `attach_argv` to the context — so the fake is wired the way production is rather than
    being free to disagree with it.
    """
    record = _record()
    context_argv = (*_ARGV, f"={record.session_id}")
    launcher = _Listing((record,), attach=" ".join(context_argv))
    context = _context(launcher)
    app = RemoteAgentsTui(context)

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("attach")
        await pilot.pause()
        status = _status(app)

    assert " ".join(context.attach_argv(str(record.session_id))) in status


async def test_an_unavailable_attach_says_why_instead_of_hiding() -> None:
    """The bot hides the affordance silently; the local surface must not repeat that."""
    record = _record()
    launcher = _Listing((record,), attach=None)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("attach")
        await pilot.pause()
        status = _status(app)

    lowered = status.casefold()
    assert "not available" in lowered or "unavailable" in lowered
    assert "pane" in lowered
    assert launcher.asked == [record.session_id]


async def test_the_attach_entry_is_offered_on_a_live_session() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,), attach="tmux attach")))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        rows = _rows(app)

    assert any("attach" in row.casefold() for row in rows)


async def test_the_affordance_is_present_even_when_the_pane_is_dead() -> None:
    """Presence is what lets the owner learn *why*; hiding it teaches nothing."""
    record = _record(SessionState.PRESERVED)
    app = RemoteAgentsTui(_context(_Listing((record,), attach=None)))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        rows = _rows(app)

    assert any("attach" in row.casefold() for row in rows)
