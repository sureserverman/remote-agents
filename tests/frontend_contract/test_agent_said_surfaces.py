"""The agent's last words, the same on both surfaces, labelled as the agent's (BL-122).

Read off what each surface renders -- the bot's detail and list messages, the local surface's
detail rows and the sessions pane's subtitle -- not off the shared builder, as the git line's
contract does. The words are the agent's claim about its own turn, kept apart from the measured
facts by their label (OD-3).

**Where each surface draws the list's line, and why they differ.** The bot's list rows are
two lines each, so the line goes under every row (cut to the row's width; the detail carries it
whole). The console's sessions pane draws one line per row with columns sized to the pane, so
the line goes in the pane's subtitle, for the highlighted row only -- the one place under the
cursor with room. The plain sessions screen of a bare `remote-agents tui` shows it on the
detail only. Same words, same source, placed by each surface's layout (DEC-033 is about the
step and the words, not the pixels); recorded here so the difference is a decision, not drift.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
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

WORDS = "Tests pass; pushed the branch"
LINE = f"agent said: {WORDS}"


def _record(slug: str = "opaque-editor") -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId(slug),
        ProfileId("claude"),
        SessionDisplayIdentity(slug, "claude", "regular", 1),
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


def _reader(words: dict[str, str], calls: list[tuple[str, ...]] | None = None):
    async def last_words(session_ids: Sequence[str]) -> dict[str, str]:
        if calls is not None:
            calls.append(tuple(session_ids))
        return {session: words[session] for session in session_ids if session in words}

    return last_words


def _plain(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


async def _telegram_detail(record: SessionRecord, words: dict[str, str]) -> list[str]:
    boundary = build_private_bot(
        7, 11, backend=backend_for(sessions=_Sessions(record), last_words=_reader(words))
    )
    detail = await boundary._detail_reply(str(record.session_id))
    return [line for line in _plain(detail.text).splitlines() if "said" in line]


async def _tui_detail(record: SessionRecord, words: dict[str, str]) -> list[str]:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=_Sessions(record),  # type: ignore[arg-type]
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=tuple,
                last_words=_reader(words),
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )
    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        rows = [str(option.prompt) for option in choices.options]
        highlighted = choices.highlighted
        # The cursor still rests on the first row that does something, past the fact rows.
        if rows and highlighted is not None:
            assert "said" not in rows[highlighted]
    return [row for row in rows if "said" in row]


SURFACES = surface_pairs(telegram=_telegram_detail, tui=_tui_detail)


@pytest.mark.parametrize("surface,detail", SURFACES)
async def test_both_details_say_what_the_agent_said_under_its_label(surface: str, detail) -> None:
    record = _record()

    assert await detail(record, {str(record.session_id): WORDS}) == [LINE], surface


@pytest.mark.parametrize("surface,detail", SURFACES)
async def test_no_finished_turn_draws_no_line(surface: str, detail) -> None:
    assert await detail(_record(), {}) == [], surface


async def test_the_bot_list_carries_the_line_under_the_row_in_one_read() -> None:
    first, second = _record("first-project"), _record("second-project")
    calls: list[tuple[str, ...]] = []

    class _Two(_Sessions):
        async def list_sessions(self):
            return (first, second)

    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            sessions=_Two(first),
            last_words=_reader({str(first.session_id): WORDS}, calls),
        ),
    )
    page = _plain((await boundary._sessions_reply(records=(first, second))).text).splitlines()

    start = next(i for i, line in enumerate(page) if line.startswith("first-project"))
    assert page[start + 2] == LINE
    other = next(i for i, line in enumerate(page) if line.startswith("second-project"))
    assert all("said" not in line for line in page[other : other + 3])
    assert calls == [(str(first.session_id), str(second.session_id))]


async def test_a_failing_read_costs_the_line_never_the_screen() -> None:
    record = _record()

    async def broken(_session_ids):
        raise RuntimeError("store gone")

    boundary = build_private_bot(
        7, 11, backend=backend_for(sessions=_Sessions(record), last_words=broken)
    )
    detail = await boundary._detail_reply(str(record.session_id))
    page = await boundary._sessions_reply(records=(record,))

    assert "said" not in _plain(detail.text)
    assert "said" not in _plain(page.text)


async def test_a_full_page_of_long_escaped_words_still_fits_one_message() -> None:
    """Eight rows at the drain's 240-character bound, every character one HTML escape expands,
    must not pass Telegram's 4096 units: the list is the only way to reach a session."""
    from remote_agents.adapters.telegram.presenters import UNIFORM_ROW_WIDTH

    records = tuple(_record(f"project-{index}") for index in range(8))
    worst = '"<&>' * 60

    class _Eight(_Sessions):
        async def list_sessions(self):
            return records

    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            sessions=_Eight(records[0]),
            last_words=_reader({str(record.session_id): worst for record in records}),
        ),
    )
    page = await boundary._sessions_reply(records=records)
    lines = [line for line in _plain(page.text).splitlines() if line.startswith("agent said")]

    assert len(lines) == 8
    assert all(len(line) <= UNIFORM_ROW_WIDTH for line in lines)
    assert all(line.endswith("…") for line in lines)
    # The detail keeps the whole line.
    detail = await boundary._detail_reply(str(records[0].session_id))
    assert f"agent said: {worst}" in _plain(detail.text)
