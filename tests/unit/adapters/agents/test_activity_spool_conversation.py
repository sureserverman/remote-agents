"""A finished turn leaves the agent's own conversation id against the session it belongs to.

The id travels hook -> spool -> drain -> ledger, and only from a turn the session's own agent
ended: a nested agent run inside a managed pane inherits the pane's session id, and its `Stop`
must not overwrite the parent's conversation. The turn marker already records which agent
started the running turn (DEC-104), so the hook keeps the id only when that agent is the one
ending it -- or when there was no marker to say otherwise.

Codex hooks are attributed from their payload through the roster (DEC-126), never from the
environment, and the conversation they leave is their own thread.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from remote_agents.adapters.agents.activity_spool import SESSION_ID_VARIABLE, spool_agent_event
from remote_agents.adapters.agents.hook_identity import RosterEntry, roster_path, write_roster
from remote_agents.adapters.agents.hook_licences import hook_licence
from remote_agents.adapters.sqlite.conversation_ledger import SQLiteConversationLedger
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.application.activity import drain_activity
from remote_agents.application.conversation_sightings import record_conversations

_SESSION = "ra-claude_1"
_PARENT = "f4020001-e712-4832-9fc8-dd28d38d5b8a"
_CHILD = "5b1d2e7a-0c3f-4a9e-8d21-6f4b9c0e1a77"
_THREAD = "01a1360a-d366-7913-abdc-27aa12faf338"
_CODEX_SESSION = "8b4f34c9-92c9-45db-bb45-1ab0c24dd7bb"
_DAEMON_STARTER = "840f2da2-543c-4299-b676-fe8d10895be3"
_NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def _spool(tmp_path: Path) -> Path:
    directory = tmp_path / "activity"
    directory.mkdir(mode=0o700)
    return directory


def _payload(event: str, agent: str, cwd: str = "/tmp/scratch") -> dict[str, Any]:
    return {
        "session_id": agent,
        "cwd": cwd,
        "hook_event_name": event,
        "prompt": "secret words that must never be stored",
        "last_assistant_message": "done",
    }


def _hook(directory: Path, payload: dict[str, Any], provider: str, environment_id: str) -> None:
    licence = hook_licence(provider)
    assert licence is not None
    spool_agent_event(
        io.BytesIO(json.dumps(payload).encode("utf-8")),
        activity_directory=directory,
        environment={SESSION_ID_VARIABLE: environment_id},
        licence=lambda: licence,
        hook_name=provider,
        now=lambda: _NOW,
    )


async def _ledgered(tmp_path: Path, directory: Path) -> SQLiteConversationLedger:
    ledger = SQLiteConversationLedger(open_database(tmp_path / "state.sqlite3"))
    await record_conversations(drain_activity(directory), ledger)
    return ledger


async def test_a_claude_turn_leaves_its_conversation_against_its_session(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    _hook(directory, _payload("UserPromptSubmit", _PARENT), "claude", _SESSION)
    _hook(directory, _payload("Stop", _PARENT), "claude", _SESSION)

    ledger = await _ledgered(tmp_path, directory)

    assert await ledger.latest(_SESSION) == _PARENT


async def test_a_nested_agents_stop_does_not_replace_the_parents_conversation(
    tmp_path: Path,
) -> None:
    directory = _spool(tmp_path)
    _hook(directory, _payload("UserPromptSubmit", _PARENT), "claude", _SESSION)
    # A `claude -p` the parent ran inherits the pane's session id; its turn ends inside the
    # parent's, which still holds the marker.
    _hook(directory, _payload("UserPromptSubmit", _CHILD), "claude", _SESSION)
    _hook(directory, _payload("Stop", _CHILD), "claude", _SESSION)

    ledger = await _ledgered(tmp_path, directory)

    assert await ledger.latest(_SESSION) is None


async def test_a_stop_with_no_marker_still_names_its_conversation(tmp_path: Path) -> None:
    """The service ends a marker on screen evidence before the hook's own Stop can arrive."""
    directory = _spool(tmp_path)
    _hook(directory, _payload("Stop", _PARENT), "claude", _SESSION)

    ledger = await _ledgered(tmp_path, directory)

    assert await ledger.latest(_SESSION) == _PARENT


async def test_a_codex_stop_is_recorded_against_the_resolved_session_not_the_environment(
    tmp_path: Path,
) -> None:
    directory = _spool(tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    write_roster(
        roster_path(directory, "codex"),
        (
            RosterEntry(
                _CODEX_SESSION, str(workspace.resolve()), datetime(2026, 10, 13, tzinfo=UTC)
            ),
        ),
    )
    _hook(directory, _payload("Stop", _THREAD, str(workspace)), "codex", _DAEMON_STARTER)

    ledger = await _ledgered(tmp_path, directory)

    assert await ledger.latest(_CODEX_SESSION) == _THREAD
    assert await ledger.latest(_DAEMON_STARTER) is None


async def test_the_spooled_record_carries_the_id_and_no_words(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    _hook(directory, _payload("Stop", _PARENT), "claude", _SESSION)

    (record,) = [json.loads(path.read_text()) for path in directory.glob("*.json")]

    assert record["conversation"] == _PARENT
    assert "secret words" not in json.dumps(record)
