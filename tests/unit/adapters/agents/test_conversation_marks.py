"""A turn's start leaves the agent's own conversation id where a restart can find it.

The resume after a restart must reopen each interrupted session's own conversation, and a
fresh launch never pins one. So the hook, at the start of a turn the session's own agent owns,
writes that agent's id to `<activity>/conversations/<session>`. Only the agent that holds the
session's turn marker after `start` writes it (DEC-104's owner): a nested agent started inside
the pane lands on its parent's running turn and never owns it, so it never overwrites the
parent's conversation. The file is replaced whole, needs no clock and no drain, and survives a
reboot. It holds an identifier and nothing typed (DEC-067).

Codex hooks are attributed from their payload through the roster (DEC-126), never from the
environment, and the conversation they leave is their own thread.
"""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from remote_agents.adapters.agents.activity_spool import (
    MAXIMUM_PAYLOAD_BYTES,
    SESSION_ID_VARIABLE,
    spool_agent_event,
)
from remote_agents.adapters.agents.hook_identity import RosterEntry, roster_path, write_roster
from remote_agents.adapters.agents.hook_licences import hook_licence
from remote_agents.adapters.agents.turn_markers import FileConversationMarks, FileTurnMarkers

_SESSION = "ra-claude_1"
_PARENT = "f4020001-e712-4832-9fc8-dd28d38d5b8a"
_CHILD = "5b1d2e7a-0c3f-4a9e-8d21-6f4b9c0e1a77"
_THREAD = "01a1360a-d366-7913-abdc-27aa12faf338"
_CODEX_SESSION = "8b4f34c9-92c9-45db-bb45-1ab0c24dd7bb"
_DAEMON_STARTER = "840f2da2-543c-4299-b676-fe8d10895be3"


def _spool(tmp_path: Path) -> Path:
    directory = tmp_path / "activity"
    directory.mkdir(mode=0o700)
    return directory


def _payload(event: str, agent: str, cwd: str = "/tmp/scratch", prompt: str = "hi") -> dict:
    return {"session_id": agent, "cwd": cwd, "hook_event_name": event, "prompt": prompt}


def _hook(directory: Path, payload: dict[str, Any], provider: str, environment_id: str) -> None:
    licence = hook_licence(provider)
    assert licence is not None
    spool_agent_event(
        io.BytesIO(json.dumps(payload).encode("utf-8")),
        activity_directory=directory,
        environment={SESSION_ID_VARIABLE: environment_id},
        licence=lambda: licence,
        hook_name=provider,
        now=lambda: datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
    )


def test_a_claude_turn_start_leaves_its_conversation(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    _hook(directory, _payload("UserPromptSubmit", _PARENT), "claude", _SESSION)

    assert FileConversationMarks(directory).latest(_SESSION) == _PARENT


def test_a_nested_agents_turn_never_replaces_the_parents_conversation(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    _hook(directory, _payload("UserPromptSubmit", _PARENT), "claude", _SESSION)
    # A `claude -p` the parent ran inherits the pane's session id; its turn starts and ends
    # inside the parent's, which holds the marker throughout.
    _hook(directory, _payload("UserPromptSubmit", _CHILD), "claude", _SESSION)
    _hook(directory, _payload("Stop", _CHILD), "claude", _SESSION)

    assert FileConversationMarks(directory).latest(_SESSION) == _PARENT


def test_a_new_conversation_in_the_same_pane_replaces_the_old_one(tmp_path: Path) -> None:
    """`/clear` or `/new`: the old turn ended, so the next submit's agent owns the next turn."""
    directory = _spool(tmp_path)
    _hook(directory, _payload("UserPromptSubmit", _PARENT), "claude", _SESSION)
    _hook(directory, _payload("Stop", _PARENT), "claude", _SESSION)
    _hook(directory, _payload("UserPromptSubmit", _CHILD), "claude", _SESSION)

    assert FileConversationMarks(directory).latest(_SESSION) == _CHILD


def test_a_prompt_past_the_payload_bound_still_leaves_its_conversation(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    long = _payload("UserPromptSubmit", _PARENT, prompt="x" * (MAXIMUM_PAYLOAD_BYTES + 10))
    _hook(directory, long, "claude", _SESSION)

    assert FileConversationMarks(directory).latest(_SESSION) == _PARENT


def test_a_codex_turn_is_marked_against_the_resolved_session_not_the_environment(
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
    submit = _payload("UserPromptSubmit", _THREAD, str(workspace))
    _hook(directory, submit, "codex", _DAEMON_STARTER)

    marks = FileConversationMarks(directory)
    assert marks.latest(_CODEX_SESSION) == _THREAD
    assert marks.latest(_DAEMON_STARTER) is None


def test_the_mark_holds_the_id_and_no_words(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    submit = _payload("UserPromptSubmit", _PARENT, prompt="secret words")
    _hook(directory, submit, "claude", _SESSION)

    for path in directory.rglob("*"):
        if path.is_file():
            assert b"secret words" not in path.read_bytes()


def test_marks_are_listed_read_and_forgotten(tmp_path: Path) -> None:
    marks = FileConversationMarks(_spool(tmp_path))
    marks.record(_SESSION, _PARENT)
    marks.record("ra-codex_2", _THREAD)

    assert sorted(marks.sessions()) == sorted([_SESSION, "ra-codex_2"])
    marks.forget(_SESSION)
    assert marks.latest(_SESSION) is None
    assert marks.sessions() == ("ra-codex_2",)


def test_a_malformed_or_foreign_mark_reads_as_none(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    marks = FileConversationMarks(directory)
    marks.record(_SESSION, _PARENT)
    target = directory / "conversations" / _SESSION
    target.write_text("has space\n")
    assert marks.latest(_SESSION) is None

    target.unlink()
    os.mkfifo(target, 0o600)
    assert marks.latest(_SESSION) is None


def test_a_link_planted_at_a_marks_name_is_replaced_never_written_through(
    tmp_path: Path,
) -> None:
    directory = _spool(tmp_path)
    marks = FileConversationMarks(directory)
    marks.record("ra-other", _CHILD)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (directory / "conversations" / _SESSION).symlink_to(victim)

    marks.record(_SESSION, _PARENT)

    assert victim.read_text() == "untouched"
    assert marks.latest(_SESSION) == _PARENT


def test_a_malformed_id_is_never_written(tmp_path: Path) -> None:
    marks = FileConversationMarks(_spool(tmp_path))
    for value in ("", "has space", "x" * 129, "line\nbreak"):
        marks.record(_SESSION, value)
    marks.record("not a session", _PARENT)
    assert marks.sessions() == ()


def test_the_turn_marker_reports_its_owner(tmp_path: Path) -> None:
    markers = FileTurnMarkers(_spool(tmp_path))
    assert markers.owner(_SESSION) is None
    markers.start(_SESSION, owner=_PARENT)
    markers.start(_SESSION, owner=_CHILD)
    assert markers.owner(_SESSION) == _PARENT


def test_an_abandoned_temporary_is_collected_and_a_fresh_one_is_left(tmp_path: Path) -> None:
    """A hook killed between its create and its rename leaves a `.pending-*` file behind."""
    directory = _spool(tmp_path)
    marks = FileConversationMarks(directory)
    marks.record(_SESSION, _PARENT)
    old = directory / "conversations" / ".pending-0123456789ab"
    fresh = directory / "conversations" / ".pending-ba9876543210"
    old.write_text(_CHILD)
    fresh.write_text(_CHILD)
    two_hours_ago = datetime.now(UTC).timestamp() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))

    assert marks.sessions() == (_SESSION,)
    assert not old.exists()
    assert fresh.exists()
