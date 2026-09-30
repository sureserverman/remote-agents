"""Live render: the bot's limits block over this host's own readers.

The block's layout is pinned headless in `tests/unit` and against the terminal's pane in
`tests/frontend_contract`. What those cannot show is the real `Backend` on this host: Claude's
status-line recording, Codex's app server, and Cursor with its switch as the owner left it.

**Not opt-in.** It reads what the running service already reads every few minutes, through the
owner's own config, and writes nothing of the owner's: the session store is a scratch file and
the chat is a fake. It calls Cursor's server only where the owner has switched that read on,
which is the call the service then makes anyway. It skips only on a host with no config, and
says so as BLOCKED.
"""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

import pytest
from fake_telegram import OWNER_CHAT_ID, OWNER_USER_ID, FakeChat

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.telegram.limits_block import TITLE, WIDTH
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.session_views import limit_rows
from remote_agents.composition.backend import compose_backend
from remote_agents.config import load_config
from remote_agents.domain.models import ProfileId
from remote_agents.production import ProductionPaths

_CREDENTIAL_FILES = (
    Path.home() / ".config" / "cursor" / "auth.json",
    Path.home() / ".claude" / ".credentials.json",
)


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for member in value.values():
            yield from _strings(member)
    elif isinstance(value, list):
        for member in value:
            yield from _strings(member)


def _tokens() -> tuple[str, ...]:
    """Every long string in the provider login files, so none of them may appear on screen."""
    found: list[str] = []
    for path in _CREDENTIAL_FILES:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found += [text for text in _strings(document) if len(text) >= 20]
    return tuple(found)


async def test_the_bot_draws_one_group_per_reporting_profile_from_the_real_readers(
    tmp_path: Path,
) -> None:
    paths = ProductionPaths.for_home(Path.home())
    if not paths.config_path.exists():
        pytest.skip(f"BLOCKED: no config at {paths.config_path}; run `remote-agents onboard`")
    config = load_config(paths.config_path)
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        backend = compose_backend(config, connection, paths)
        assert backend.limits is not None, "this host wired no limits reader"
        profiles = tuple(ProfileId(profile.profile_id) for profile in backend.profiles)
        assert profiles, "this host offers no agent"
        # Read directly first: a read that raises would still draw a block, every row
        # `unreadable`, and this test is about the block a working read draws.
        expected = limit_rows(await backend.limits(), profiles)
        boundary = build_private_bot(
            OWNER_USER_ID, OWNER_CHAT_ID, backend=backend, profiles=backend.profiles
        )
        chat = FakeChat()

        await boundary.sessions_command(chat.message_update("/sessions"), None)
    finally:
        connection.close()

    text = chat.bot_messages[-1].text
    assert f"<b>{TITLE}</b>" in text, text
    block = text.split(f"<b>{TITLE}</b>\n", 1)[1].split("\n\n", 1)[0]
    lines = block.split("\n")
    headings = [line for line in lines if not line.startswith("<code>")]
    assert headings == [row.profile for row in expected], (headings, expected)
    assert {row.profile for row in expected} <= {str(profile) for profile in profiles}
    for row in expected:
        group = lines[lines.index(row.profile) + 1 :]
        assert group and group[0].startswith("<code>"), f"{row.profile} has a name and no line"
    monospace = [
        unescape(line.removeprefix("<code>").removesuffix("</code>"))
        for line in lines
        if line.startswith("<code>")
    ]
    assert all(len(line) <= WIDTH for line in monospace), monospace
    assert re.search(r"Traceback|Error\b|Exception|\bNone\b", block) is None, block
    for token in _tokens():
        assert token not in text, "a credential reached the screen"
