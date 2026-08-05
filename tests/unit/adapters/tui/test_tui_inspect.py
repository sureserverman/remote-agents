"""Captured output is shown locally through the shared sanitizer, not the bot's wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
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

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


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

    async def refresh_readiness(self):
        return self.records

    async def list_sessions(self):
        return self.records

    async def copy_attach(self, _session_id):
        return None


def _context(launcher: _Listing, capture=None, redactions: tuple[str, ...] = ()) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
        capture=capture,
        capture_redactions=redactions,
    )


def _status(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#status").content)


def _output(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#output").content)


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [getattr(item, "entry_key", None) for item in app.query("ListView > ListItem")]


def _capturing(text: str):
    async def capture(_session_id: SessionId) -> str:
        return text

    return capture


async def test_inspect_renders_the_captured_output() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("Claude Code ready\nline two")))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("inspect")
        await pilot.pause()
        step = app._step
        output = _output(app)

    assert step is Step.INSPECT
    assert "Claude Code ready" in output
    assert "line two" in output


async def test_ansi_escapes_are_stripped_by_the_shared_sanitizer() -> None:
    record = _record()
    raw = "\x1b[31mred\x1b[0m text"
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(raw)))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("inspect")
        await pilot.pause()
        output = _output(app)

    assert "\x1b" not in output
    assert "red text" in output


async def test_configured_redactions_are_applied() -> None:
    record = _record()
    app = RemoteAgentsTui(
        _context(_Listing((record,)), _capturing("token=hunter2 rest"), redactions=("hunter2",))
    )

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("inspect")
        await pilot.pause()
        output = _output(app)

    assert "hunter2" not in output
    assert "[REDACTED]" in output


async def test_binary_output_containing_a_nul_byte_is_refused() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("before\x00after")))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("inspect")
        await pilot.pause()
        output = _output(app)
        status = _status(app)

    assert "after" not in output
    assert "binary" in (output + status).casefold()


async def test_no_telegram_limit_or_attachment_fallback_reaches_the_local_surface() -> None:
    """The bot truncates at 4096 UTF-16 units and falls back to a file; the TUI scrolls."""
    record = _record()
    long_output = "\n".join(f"line {index}" for index in range(400))
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(long_output)))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("inspect")
        await pilot.pause()
        output = _output(app)
        status = _status(app)

    assert "line 399" in output, "the local surface must not truncate at the Telegram limit"
    assert "session-output.txt" not in (output + status)
    assert "attachment" not in (output + status).casefold()


def test_the_tui_imports_nothing_from_the_telegram_inspection_wrapper() -> None:
    source = Path("src/remote_agents/adapters/tui/app.py").read_text(encoding="utf-8")
    assert "inspection" not in source
    assert "telegram" not in source


async def test_a_context_without_capture_offers_no_inspect_entry() -> None:
    """A host with no capture wired must not render an affordance that cannot work."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), capture=None))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        keys = _keys(app)
        await app._resolve_detail("inspect")
        await pilot.pause()
        step = app._step

    assert "inspect" not in keys
    assert step is Step.SESSION_DETAIL


async def test_a_failing_capture_reports_itself_rather_than_crashing() -> None:
    record = _record()

    async def exploding(_session_id):
        raise RuntimeError("pane is gone")

    app = RemoteAgentsTui(_context(_Listing((record,)), exploding))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("inspect")
        await pilot.pause()
        status = _status(app)

    assert "pane is gone" in status


async def test_escape_returns_from_inspect_to_the_detail() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("output")))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("inspect")
        await pilot.pause()
        await app.action_back()
        await pilot.pause()
        step = app._step

    assert step is Step.SESSION_DETAIL


async def test_leaving_inspect_by_any_route_restores_the_list() -> None:
    """Escape is not the only way out: Ctrl+S and Ctrl+R also leave this screen."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("output")))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("inspect")
        await pilot.pause()
        assert app.query_one("#choices").display is False

        await pilot.press("ctrl+s")
        await pilot.pause()
        choices_visible = app.query_one("#choices").display
        output_visible = app.query_one("#output-pane").display
        step = app._step

    assert step is Step.SESSIONS
    assert choices_visible is True, "the session list is invisible after leaving inspect"
    assert output_visible is False
