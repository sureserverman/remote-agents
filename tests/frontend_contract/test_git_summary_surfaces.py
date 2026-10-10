"""The git line and the row token, the same on both surfaces (BL-117, DEC-091).

Read off what each surface actually renders -- the bot's detail and list messages, the local
surface's detail rows and its row content -- not off the shared builder, which would agree
with itself whatever the surfaces did.
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
from remote_agents.adapters.tui.rows import session_content
from remote_agents.application.session_views import (
    SessionGit,
    git_line,
    session_row_parts,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.git_checkout import GitSummary

READ = SessionGit(GitSummary("ra/3f2a1b2c", 6, 182, 74, 2, True), shared=False)
LINE = "ra/3f2a1b2c · 6 files +182 −74 · 2 ahead · dirty"


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


def _reader(read: SessionGit | None):
    async def git_summary(_session_id: SessionId) -> SessionGit | None:
        return read

    return git_summary


def _plain(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


async def _telegram_detail_git(record: SessionRecord, read: SessionGit | None) -> list[str]:
    boundary = build_private_bot(
        7, 11, backend=backend_for(sessions=_Sessions(record), git_summary=_reader(read))
    )
    detail = await boundary._detail_reply(str(record.session_id))
    return [
        line.removeprefix("git").strip()
        for line in _plain(detail.text).splitlines()
        if line.startswith("git ")
    ]


async def _tui_detail_git(record: SessionRecord, read: SessionGit | None) -> list[str]:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    class _Launcher(_Sessions):
        async def refresh_readiness(self):
            return (self.record,)

        async def copy_attach(self, _session_id):
            return None

    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=_Launcher(record),  # type: ignore[arg-type]
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=tuple,
                git_summary=_reader(read),
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
    return [row.removeprefix("git").strip() for row in rows if row.startswith("git ")]


SURFACES = surface_pairs(telegram=_telegram_detail_git, tui=_tui_detail_git)


@pytest.mark.parametrize("surface,detail", SURFACES)
async def test_both_surfaces_render_the_same_git_line(surface: str, detail) -> None:
    assert await detail(_record(), READ) == [LINE], surface


@pytest.mark.parametrize("surface,detail", SURFACES)
async def test_a_shared_folder_says_it_is_the_whole_checkout(surface: str, detail) -> None:
    shared = SessionGit(READ.summary, shared=True)
    assert await detail(_record(), shared) == [git_line(shared.summary, shared=True)], surface
    assert git_line(shared.summary, shared=True).startswith("checkout: ")


@pytest.mark.parametrize("surface,detail", SURFACES)
async def test_no_summary_renders_no_line_and_no_error(surface: str, detail) -> None:
    assert await detail(_record(), None) == [], surface


# -- the row token, at each surface's narrow width ---------------------------------------------


async def _telegram_row(record: SessionRecord, read: SessionGit | None) -> tuple[str, str]:
    boundary = build_private_bot(
        7, 11, backend=backend_for(sessions=_Sessions(record), git_summary=_reader(read))
    )
    page = _plain((await boundary._sessions_reply(records=(record,))).text).splitlines()
    first = next(
        index for index, line in enumerate(page) if line.startswith(record.display.project_slug)
    )
    return page[first], page[first + 1]


async def test_the_bot_row_carries_the_token_after_the_identity_and_state() -> None:
    record = _record()
    identity, state = await _telegram_row(record, READ)

    assert identity.startswith("opaque-editor")
    assert "±" not in identity
    assert state.endswith(" · ±6")


def test_the_bot_row_drops_the_token_whole_when_the_line_would_pass_its_width() -> None:
    from remote_agents.adapters.telegram.presenters import UNIFORM_ROW_WIDTH
    from remote_agents.adapters.telegram.service import session_state_line

    record = _record()
    bare = session_state_line(record, None, None, "")
    room = UNIFORM_ROW_WIDTH - len(bare) - len(" · ")
    fits, too_long = "±" + "9" * (room - 1), "±" + "9" * room

    assert session_state_line(record, None, None, fits) == f"{bare} · {fits}"
    assert session_state_line(record, None, None, too_long) == bare


@pytest.mark.parametrize("width", [36, 44, 80])
def test_the_local_row_never_cuts_the_identity_for_the_token(width: int) -> None:
    record = _record("a-rather-long-project-slug")
    plain = session_row_parts(record)
    tokened = session_row_parts(record, git=READ.summary)
    assert tokened.git == "±6"

    without = session_content(plain, width=width, state_width=7, age_width=3, gauge_width=4).plain
    with_token = session_content(
        tokened, width=width, state_width=7, age_width=3, gauge_width=4
    ).plain

    identity = plain.identity
    # Whatever of the identity fitted without the token still fits with it.
    shown = without.split("  ")[0].strip()
    assert shown in with_token
    if "±6" in with_token:
        assert with_token.index(identity) < with_token.index("±6")
    if width >= 80:
        assert "±6" in with_token
    if width == 36:
        assert "±6" not in with_token


def test_at_every_width_the_local_row_shows_the_whole_token_or_exactly_the_row_without_it() -> None:
    """Never `name · …` or a lone `±`: where the token does not fit whole, the row is the
    token-less row, cell for cell -- the rule the bot's `with_token` applies to its line."""
    record = _record("editor")
    plain = session_row_parts(record)
    tokened = session_row_parts(record, git=READ.summary)
    for width in range(20, 61):
        without = session_content(plain, width=width, state_width=7, age_width=3, gauge_width=4)
        with_token = session_content(
            tokened, width=width, state_width=7, age_width=3, gauge_width=4
        )
        assert "±6" in with_token.plain or with_token.plain == without.plain, width
