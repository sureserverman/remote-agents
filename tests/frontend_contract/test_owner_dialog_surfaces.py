"""A dialog only the owner can answer, named the same on both surfaces, with nothing to press.

Codex 0.171.0 can open a session on "Hooks need review" (BL-146). The service presses nothing on
it, so both details say `waits on a hook review - answer it in its pane`, and the bot offers no
folder-trust row for it -- not on the detail, not on the trust question it sends -- even when
the record is UNTRUSTED from the folder-trust question answered just before. A folder-trust
dialog still gets its row. Read off what each surface renders.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for
from surfaces import surface_pairs
from textual.widgets import OptionList

from remote_agents.adapters.agents.registry import profile_trust_dialogs
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.trust import TrustState

LINE = "waits on a hook review - answer it in its pane"


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("codex"),
        SessionDisplayIdentity("opaque-editor", "codex", "regular", 1),
        state,
        datetime.now(UTC),
    )


class _Sessions(SessionUseCaseDouble):
    def __init__(
        self, record: SessionRecord, *, dialog: str | None, trust: TrustState = TrustState.UNKNOWN
    ) -> None:
        self.record = record
        self.dialog = dialog
        self.trust = trust

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

    async def trust_state(self, _session_id):
        return self.trust

    async def owner_dialog(self, _session_id):
        return self.dialog


def _plain(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def _buttons(detail) -> list[str]:
    return [button.text for row in detail.keyboard for button in row]


async def _telegram(sessions: _Sessions) -> tuple[list[str], list[str]]:
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(sessions=sessions),
        trust_dialogs=profile_trust_dialogs(),
    )
    detail = await boundary.sessions._detail_reply(str(sessions.record.session_id))
    # The bot labels each fact (`dialog   …`, as `git` and `context`); the words are after it.
    lines = [
        re.sub(r"^dialog\s+", "", line)
        for line in _plain(detail.text).splitlines()
        if "waits on" in line
    ]
    return lines, _buttons(detail)


async def _telegram_detail(sessions: _Sessions) -> list[str]:
    return (await _telegram(sessions))[0]


async def _tui_detail(sessions: _Sessions) -> list[str]:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=sessions,  # type: ignore[arg-type]
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=tuple,
            ),
            profiles=(ProfileAvailability("codex", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )
    async with app.run_test() as pilot:
        await app.show_detail(str(sessions.record.session_id))
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        rows = [str(option.prompt) for option in choices.options]
        highlighted = choices.highlighted
        # A fact row: the cursor still rests on the first row that does something.
        if rows and highlighted is not None:
            assert "waits on" not in rows[highlighted]
    return [row for row in rows if "waits on" in row]


SURFACES = surface_pairs(telegram=_telegram_detail, tui=_tui_detail)


@pytest.mark.parametrize("surface,detail", SURFACES)
@pytest.mark.parametrize("state", [SessionState.RUNNING, SessionState.UNTRUSTED])
async def test_both_details_say_the_session_waits_on_a_hook_review(
    surface: str, detail, state: SessionState
) -> None:
    sessions = _Sessions(_record(state), dialog="a hook review")

    assert await detail(sessions) == [LINE], surface


@pytest.mark.parametrize("surface,detail", SURFACES)
async def test_no_owner_dialog_draws_no_line(surface: str, detail) -> None:
    assert await detail(_Sessions(_record(), dialog=None)) == [], surface


@pytest.mark.parametrize("surface,detail", SURFACES)
async def test_an_ended_session_is_not_read_for_one(surface: str, detail) -> None:
    sessions = _Sessions(_record(SessionState.ENDED), dialog="a hook review")

    assert await detail(sessions) == [], surface


async def test_the_bot_offers_no_trust_row_while_an_owner_dialog_is_up() -> None:
    """Even on an UNTRUSTED record whose trust reading says AWAITING: the drawn dialog wins."""
    sessions = _Sessions(
        _record(SessionState.UNTRUSTED), dialog="a hook review", trust=TrustState.AWAITING
    )

    lines, buttons = await _telegram(sessions)

    assert lines == [LINE]
    # Neither answer: not "Trust this project", not "Don't trust — close it".
    assert not any("trust" in text.lower() for text in buttons), buttons


async def test_a_folder_trust_dialog_still_gets_the_trust_row() -> None:
    sessions = _Sessions(_record(SessionState.UNTRUSTED), dialog=None, trust=TrustState.AWAITING)

    lines, buttons = await _telegram(sessions)

    assert lines == []
    assert any("Trust" in text for text in buttons), buttons


async def test_a_failing_read_costs_only_the_line() -> None:
    class _Raising(_Sessions):
        async def owner_dialog(self, _session_id):
            raise RuntimeError("tmux went away")

    lines, buttons = await _telegram(_Raising(_record(), dialog=None))

    assert lines == []
    assert buttons
