"""The Sessions keyboard's picker label, which is all the owner has to tell two agents apart.

The list's *text* has carried the agent's name for as long as the two-line row has existed;
the buttons under it carried state, sequence and project slug and nothing else, so two
sessions of different agents in one project were one tap apart from being told apart. The
mark closes that, and this module pins where it sits and what happens when there is none.

Marks here are stand-ins (`◆`, `▲`), never the four the verticals declare. That is the
assertion, not laziness: the bot renders the token it was handed (DEC-043), so a test
supplying its own catches the day someone answers the ask with a dict of literals in the
adapter -- which would pass a test written against the real marks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from backends import SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_views import state_emoji
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = ProjectId("a" * 24)


def _record(sequence: int, profile: str, agent_label: str) -> SessionRecord:
    # The slug goes in raw and comes back as the catalogue's name ("Demo"): rendering a
    # session under a readable project name is `session_views`' job, done before the
    # keyboard is built, and the mark has to land beside that rather than beside the
    # 24-character opaque id.
    return SessionRecord(
        SessionId(UUID(int=sequence)),
        _PROJECT,
        ProfileId(profile),
        SessionDisplayIdentity("demo", agent_label, "regular", sequence, None),
        SessionState.RUNNING,
        datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
    )


class _Listing(SessionUseCaseDouble):
    def __init__(self, *records: SessionRecord) -> None:
        self.records = list(records)

    async def list_sessions(self):
        return self.records

    async def refresh_readiness(self) -> None:
        return None


def _boundary(listing: _Listing, glyphs: dict[str, str]):
    return build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject(str(_PROJECT), "Demo", "tests", "Registered"),),
            sessions=listing,
        ),
        glyphs=glyphs,
    )


async def _picker_labels(boundary) -> list[str]:
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    return [
        unpadded(button.text)
        for row in chat.messages[anchor].reply_markup.inline_keyboard
        for button in row
    ]


async def test_two_agents_in_one_project_get_two_different_buttons() -> None:
    """The ask, stated as a test: same project, same state, same slug — one tap, two agents."""
    listing = _Listing(_record(1, "claude", "Claude"), _record(2, "codex", "Codex"))
    labels = await _picker_labels(_boundary(listing, {"claude": "◆", "codex": "▲"}))

    running = state_emoji(SessionState.RUNNING)
    assert f"{running} ◆ #1 Demo" in labels
    assert f"{running} ▲ #2 Demo" in labels


async def test_the_label_puts_the_mark_between_the_state_and_the_sequence() -> None:
    """Position is the contract: state first, because the state group is what the eye scans."""
    listing = _Listing(_record(1, "claude", "Claude"))
    (label,) = [
        text for text in await _picker_labels(_boundary(listing, {"claude": "◆"})) if "#1" in text
    ]
    assert label == f"{state_emoji(SessionState.RUNNING)} ◆ #1 Demo"


async def test_a_profile_with_no_mark_renders_the_label_it_always_did() -> None:
    """Byte-identical to the pre-glyph label — no orphaned separator, no double space.

    This is the branch every composition that wires no registry takes, and the one a fifth
    provider takes for as long as it declares nothing. It has to render, not merely not
    crash.
    """
    listing = _Listing(_record(1, "claude", "Claude"))
    for glyphs in ({}, {"codex": "▲"}, {"claude": ""}):
        labels = await _picker_labels(_boundary(listing, glyphs))
        assert f"{state_emoji(SessionState.RUNNING)} #1 Demo" in labels, glyphs
