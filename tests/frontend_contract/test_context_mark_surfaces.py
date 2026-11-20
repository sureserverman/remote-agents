"""The owner's rollover threshold, marked the same way on both surfaces (BL-119, DEC-033).

Read off what each surface renders: the bot's list row and detail, the local surface's row and
detail. Nothing is typed and nothing is pushed: the mark is a line, at or over the profile's
figure, for a running session whose context ceiling is known.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for
from surfaces import surface_pairs
from textual.widgets import OptionList

from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_usage import AgentUsage, ContextWindow

MARK = "context 83% - roll over?"


def _record(slug: str = "opaque-editor", profile: str = "claude") -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId(slug),
        ProfileId(profile),
        SessionDisplayIdentity(slug, profile, "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


class _Sessions(SessionUseCaseDouble):
    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list_sessions(self):
        return (self.record,)

    async def get(self, _session_id):
        return self.record

    async def inspect(self, _query):
        return None

    async def refresh_readiness(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return None


def _usage(used: int, limit: int | None = 1_000_000):
    async def usage(_session_id: SessionId) -> AgentUsage:
        return AgentUsage(context=ContextWindow(used, limit))

    return usage


def _plain(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


async def _telegram(record: SessionRecord, used: int, mark_at: dict[str, int]) -> str:
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(sessions=_Sessions(record), usage=_usage(used), mark_at=mark_at),
    )
    page = _plain((await boundary._sessions_reply(records=(record,))).text)
    detail = _plain((await boundary._detail_reply(str(record.session_id))).text)
    return page + "\n" + detail


async def _tui(record: SessionRecord, used: int, mark_at: dict[str, int]) -> str:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=_Sessions(record),  # type: ignore[arg-type]
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=tuple,
                usage=_usage(used),
                mark_at=mark_at,
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )
    async with app.run_test(size=(160, 40)) as pilot:
        records = await app.read_sessions()
        await app.refresh_context_windows(records)
        row = app.row_parts(record).note or ""
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        detail = "\n".join(str(option.prompt) for option in choices.options)
    return row + "\n" + detail


SURFACES = surface_pairs(telegram=_telegram, tui=_tui)


@pytest.mark.parametrize("surface,render", SURFACES)
async def test_a_session_over_its_figure_is_marked_on_the_row_and_the_detail(
    surface: str, render
) -> None:
    shown = await render(_record(), 830_000, {"claude": 80})

    assert shown.count(MARK) == 2, (surface, shown)


@pytest.mark.parametrize("surface,render", SURFACES)
async def test_a_session_under_its_figure_is_not_marked(surface: str, render) -> None:
    shown = await render(_record(), 790_000, {"claude": 80})

    assert "roll over?" not in shown, surface


@pytest.mark.parametrize("surface,render", SURFACES)
async def test_no_threshold_marks_nothing(surface: str, render) -> None:
    assert "roll over?" not in await render(_record(), 990_000, {}), surface


@pytest.mark.parametrize("surface,render", SURFACES)
async def test_another_profile_s_figure_does_not_mark_this_one(surface: str, render) -> None:
    assert "roll over?" not in await render(_record(), 990_000, {"codex": 50}), surface
