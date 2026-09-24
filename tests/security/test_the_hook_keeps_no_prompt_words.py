"""A `UserPromptSubmit` hands the hook the owner's prompt; no word of it may be kept.

The payload's `prompt` field is the owner's own words, and nothing this project renders uses
them (DEC-067's test: a field is admitted because something renders it). So the claim is about
the disk, not about a parser: after the hook has run, and after the service's drain has read
what the hook left, a sentinel planted in the prompt is found in **no file** under the activity
directory -- whatever the hook chose to write there.

It is also the rollback property (BL-108's plan, Task 1.1). A release that installs a
`UserPromptSubmit` group can be rolled back to one that does not know the event, and the group
stays installed. On that older code this event must still keep nothing and deliver nothing.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.activity_spool import SESSION_ID_VARIABLE, spool_agent_event
from remote_agents.application.activity import drain_activity

_SENTINEL = "zq-sentinel-7c1e5b-never-persist"

#: As Claude Code 2.1.282 sends it (hooks reference; measured 2026-09-24), with the sentinel as
#: the owner's prompt. Codex 0.155.1 sends the same event name and a `prompt` field.
_SUBMIT: dict[str, object] = {
    "session_id": "f4020001-e712-4832-9fc8-dd28d38d5b8a",
    "transcript_path": "/home/user/.claude/projects/infra/f4020001.jsonl",
    "cwd": "/tmp/scratch",
    "permission_mode": "auto",
    "hook_event_name": "UserPromptSubmit",
    "prompt": f"please count to ten {_SENTINEL}",
}


def _spool(tmp_path: Path) -> Path:
    directory = tmp_path / "activity"
    directory.mkdir(mode=0o700)
    return directory


def _every_byte_under(directory: Path) -> bytes:
    return b"".join(path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file())


def _submit(directory: Path, provider: str) -> int:
    return spool_agent_event(
        io.BytesIO(json.dumps(_SUBMIT).encode("utf-8")),
        activity_directory=directory,
        environment={SESSION_ID_VARIABLE: "s-42"},
        now=lambda: datetime(2026, 9, 24, 21, 25, tzinfo=UTC),
        provider=provider,
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_submitted_prompt_is_kept_nowhere_and_delivers_nothing(
    provider: str, tmp_path: Path
) -> None:
    directory = _spool(tmp_path)

    assert _submit(directory, provider) == 0
    assert _SENTINEL.encode() not in _every_byte_under(directory)

    assert drain_activity(directory) == ()
    assert _SENTINEL.encode() not in _every_byte_under(directory)


def test_codex_writes_nothing_at_all_for_a_submit(tmp_path: Path) -> None:
    """Codex's branch admits only `Stop` and `PermissionRequest`; a submit leaves no file."""
    directory = _spool(tmp_path)

    _submit(directory, "codex")

    assert [path for path in directory.rglob("*") if path.is_file()] == []


def test_the_sentinel_detector_can_fire(tmp_path: Path) -> None:
    """A sweep that could never find the sentinel would pass every test above vacuously."""
    directory = _spool(tmp_path)
    (directory / "planted.json").write_text(json.dumps(_SUBMIT), encoding="utf-8")

    assert _SENTINEL.encode() in _every_byte_under(directory)
