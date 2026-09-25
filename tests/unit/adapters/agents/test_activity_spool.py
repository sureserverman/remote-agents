"""Unit tests for the agent hook's private activity spool."""

from __future__ import annotations

import io
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from remote_agents.adapters.agents.activity_spool import (
    _PLAIN_TOKEN,
    MAXIMUM_PAYLOAD_BYTES,
    SESSION_ID_VARIABLE,
    spool_agent_event,
)
from remote_agents.ports.agent_activity import MAXIMUM_DETAIL_CHARACTERS

_STOP_PAYLOAD: dict[str, Any] = {
    "session_id": "f4020001-e712-4832-9fc8-dd28d38d5b8a",
    "transcript_path": "/home/user/.claude/projects/infra/f4020001.jsonl",
    "cwd": "/tmp/scratch",
    "prompt_id": "eed32c54-420b-475a-a542-35db69c102b6",
    "permission_mode": "auto",
    "effort": {"level": "high"},
    "hook_event_name": "Stop",
    "stop_hook_active": False,
    "last_assistant_message": "probe",
    "background_tasks": [],
    "session_crons": [],
}


def _clock(moment: datetime = datetime(2026, 8, 11, 14, 22, 33, 123456, tzinfo=UTC)):
    return lambda: moment


def _spool(tmp_path: Path) -> Path:
    directory = tmp_path / "activity"
    directory.mkdir(mode=0o700)
    return directory


def _stream(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _record(directory: Path) -> dict[str, Any]:
    entries = sorted(directory.iterdir())
    assert len(entries) == 1
    return json.loads(entries[0].read_text(encoding="utf-8"))


def _run(stream: io.BytesIO, directory: Path, session_id: str | None = "s-42", **overrides) -> int:
    environment = {} if session_id is None else {SESSION_ID_VARIABLE: session_id}
    arguments = {"now": _clock()}
    arguments.update(overrides)
    return spool_agent_event(
        stream, activity_directory=directory, environment=environment, **arguments
    )


def test_a_stop_payload_writes_exactly_one_private_file_named_by_session_and_time(
    tmp_path: Path,
) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(_STOP_PAYLOAD), directory) == 0

    entries = sorted(directory.iterdir())
    assert len(entries) == 1
    assert entries[0].name.startswith("s-42-")
    assert "20260811T142233123456Z" in entries[0].name
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600


def test_a_stop_payload_keeps_only_the_fields_the_notification_needs(tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    _run(_stream(_STOP_PAYLOAD), directory)

    assert _record(directory) == {
        "session_id": "s-42",
        "event": "Stop",
        "reason": None,
        "detail": "probe",
        "observed_at": "2026-08-11T14:22:33.123456+00:00",
        # `ask` joined the record on 2026-09-06 (DEC-074) — the class of thing an agent is
        # waiting on, kept apart from `detail` because a provider token is not the agent's
        # words. `None` here because a `Stop` is not waiting on anything, and because this
        # payload is Claude's, whose branch never populates it.
        "ask": None,
    }


def test_a_stop_payload_never_spools_the_filesystem_layout_it_carries(tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    _run(_stream(_STOP_PAYLOAD), directory)

    spooled = sorted(directory.iterdir())[0].read_text(encoding="utf-8")
    assert "transcript_path" not in spooled
    assert "/tmp/scratch" not in spooled


@pytest.mark.parametrize(
    ("payload", "event", "reason", "detail"),
    [
        (
            {"hook_event_name": "StopFailure", "error": "rate_limit"},
            "StopFailure",
            "rate_limit",
            None,
        ),
        (
            {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": "Claude needs your permission to use Bash",
            },
            "Notification",
            "permission_prompt",
            "Claude needs your permission to use Bash",
        ),
        (
            {"hook_event_name": "SessionEnd", "reason": "logout"},
            "SessionEnd",
            "logout",
            None,
        ),
    ],
)
def test_each_hook_event_spools_its_own_discriminating_field(
    tmp_path: Path, payload: dict[str, Any], event: str, reason: str, detail: str | None
) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(payload), directory) == 0

    record = _record(directory)
    assert (record["event"], record["reason"], record["detail"]) == (event, reason, detail)


def test_a_detail_line_is_bounded_and_single_lined(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    message = "first line\n\tsecond   line " + "x" * MAXIMUM_DETAIL_CHARACTERS

    _run(_stream(_STOP_PAYLOAD | {"last_assistant_message": message}), directory)

    detail = _record(directory)["detail"]
    assert len(detail) == MAXIMUM_DETAIL_CHARACTERS
    assert detail.startswith("first line second line x")
    assert "\n" not in detail and "\t" not in detail


@pytest.mark.parametrize("session_id", [None, ""])
def test_without_the_session_variable_the_hook_writes_nothing_and_exits_zero(
    tmp_path: Path, session_id: str | None
) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(_STOP_PAYLOAD), directory, session_id=session_id) == 0

    assert list(directory.iterdir()) == []


@pytest.mark.parametrize(
    "session_id", ["../escape", "a/b", "..", "s\x0042", "s 42", "s\n42", "x" * 200]
)
def test_an_unsafe_session_variable_can_never_become_a_path(
    tmp_path: Path, session_id: str
) -> None:
    directory = _spool(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    assert _run(_stream(_STOP_PAYLOAD), directory, session_id=session_id) == 0

    assert list(directory.iterdir()) == []
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json at all", b'{"hook_event_name": ', b"[1, 2, 3]", b'"Stop"', b"null"],
)
def test_a_malformed_payload_writes_nothing_and_exits_zero(tmp_path: Path, raw: bytes) -> None:
    directory = _spool(tmp_path)

    assert _run(io.BytesIO(raw), directory) == 0

    assert list(directory.iterdir()) == []


def test_a_payload_without_a_hook_event_name_writes_nothing(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    payload = dict(_STOP_PAYLOAD)
    del payload["hook_event_name"]

    assert _run(_stream(payload), directory) == 0

    assert list(directory.iterdir()) == []


def test_an_oversized_payload_is_neither_spooled_nor_read_unboundedly(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    oversized = _STOP_PAYLOAD | {"last_assistant_message": "x" * (MAXIMUM_PAYLOAD_BYTES * 4)}
    stream = _stream(oversized)

    assert _run(stream, directory) == 0

    assert list(directory.iterdir()) == []
    assert stream.tell() <= MAXIMUM_PAYLOAD_BYTES + 1


def test_an_unwritable_spool_exits_zero_without_raising(tmp_path: Path) -> None:
    blocked = tmp_path / "activity"
    blocked.write_text("a regular file stands where the spool should be", encoding="utf-8")

    assert _run(_stream(_STOP_PAYLOAD), blocked) == 0


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_a_spool_that_cannot_be_created_exits_zero_without_raising(tmp_path: Path) -> None:
    # A spool the owner merely tightened is not this case: the owner can always widen its own
    # directory again, and the guard does, back to the 0700 the state directory declares. The
    # reachable failure is a spool that cannot be created at all, which is a parent that
    # refuses it.
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        assert _run(_stream(_STOP_PAYLOAD), blocked / "activity") == 0
        assert not (blocked / "activity").exists()
    finally:
        blocked.chmod(0o700)


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_a_loosened_spool_is_returned_to_the_declared_mode_before_a_record_lands(
    tmp_path: Path,
) -> None:
    directory = _spool(tmp_path)
    directory.chmod(0o755)

    assert _run(_stream(_STOP_PAYLOAD), directory) == 0

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert len(list(directory.iterdir())) == 1


def test_a_symlinked_spool_is_refused_rather_than_written_through(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    planted = tmp_path / "activity"
    planted.symlink_to(elsewhere, target_is_directory=True)

    assert _run(_stream(_STOP_PAYLOAD), planted) == 0

    assert list(elsewhere.iterdir()) == []


def test_two_events_in_the_same_tick_do_not_overwrite_each_other(tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    _run(_stream(_STOP_PAYLOAD), directory)
    _run(_stream(_STOP_PAYLOAD | {"last_assistant_message": "second"}), directory)

    entries = sorted(directory.iterdir())
    assert len(entries) == 2
    assert {json.loads(entry.read_text(encoding="utf-8"))["detail"] for entry in entries} == {
        "probe",
        "second",
    }


def test_the_drain_is_never_stricter_than_the_spool_about_an_ask_token() -> None:
    """The two ends of the spool hold the same pattern, and nothing pinned them together.

    `activity_spool._PLAIN_TOKEN` bounds what a hook may WRITE; `application.activity._ASK_TOKEN`
    bounds what the drain will READ. They are deliberately two copies rather than one shared
    constant, and that argument is sound: a different process wrote the file, so the far end
    revalidates untrusted input rather than trusting the writer — the same reasoning
    `MAXIMUM_DETAIL_CHARACTERS` is written down for.

    **What the argument does not buy is silence when they diverge.** Widen the spool's pattern
    alone — to admit a longer tool name, say — and the drain quietly rejects what the spool now
    legitimately writes: a working ask degrades to `None`, no test fails anywhere, because each
    side's tests only ever exercise its own pattern. That is the shape of the `error_type` /
    `end_reason` incident this module's own comment records, and of the allow-list mutant that
    survived earlier in this same stage.

    The asserted direction is the one that matters: **everything the spool can write, the drain
    must accept.** The reverse is harmless — a drain that accepts more than any writer produces
    rejects nothing real.
    """
    from remote_agents.application.activity import _ASK_TOKEN

    writable = [
        "Bash",
        "a",
        "A" * 64,
        "Some_Tool-42",
        "-",
        "_",
        "0",
        "web-fetch_2",
    ]
    for token in writable:
        assert _PLAIN_TOKEN.fullmatch(token), f"fixture {token!r} is not spool-writable"
        assert _ASK_TOKEN.fullmatch(token), (
            f"the spool can write {token!r} and the drain refuses it — the two patterns have "
            "drifted, and a real ask would silently become None"
        )

    # And the bound itself, since a length change is the likeliest divergence.
    assert _PLAIN_TOKEN.fullmatch("A" * 64) and _ASK_TOKEN.fullmatch("A" * 64)
    assert not _PLAIN_TOKEN.fullmatch("A" * 65) and not _ASK_TOKEN.fullmatch("A" * 65)


# --- Codex `PermissionRequest` detail (DEC-098) ---------------------------------------------
#
# Every payload below is shaped from a real capture recorded in
# `docs/acceptance-2026-09-19-ask-payloads.md`, taken against codex-cli 0.154.0 in a disposable
# home. The key names are that measurement's, not a guess: `tool_input` is nested, `Bash` carries
# `command` + `description`, and `apply_patch` carries `command` alone.

_CODEX_PERMISSION_PAYLOAD: dict[str, Any] = {
    "session_id": "01a0b94c-92b7-7d13-a0ce-876ef3fa386d",
    "turn_id": "01a0b94c-e43c-7d50-8b98-1e1836eeccdd",
    "transcript_path": "/home/user/.codex/sessions/2026/09/19/rollout.jsonl",
    "cwd": "/home/user/workspace",
    "hook_event_name": "PermissionRequest",
    "model": "gpt-5.6-sol",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {
        "command": "whoami > /tmp/ra-drill-probe.txt",
        "description": "Do you want to allow the exact command to write /tmp/ra-drill-probe.txt?",
    },
}


def _codex_permission(tool_name: str = "Bash", **tool_input: Any) -> dict[str, Any]:
    payload = {**_CODEX_PERMISSION_PAYLOAD, "tool_name": tool_name}
    payload["tool_input"] = dict(tool_input)
    return payload


def test_a_codex_permission_request_says_what_it_is_asking(tmp_path: Path) -> None:
    """The reversal DEC-098 records: the agent's own reason, and the command it is about.

    Before this, 95 of 95 Codex `needs_answer` rows read `ask=Bash` and carried no detail at
    all -- "waiting for an answer about a shell command", which on a phone cannot tell an
    `rm -rf` from an `ls`.
    """
    directory = _spool(tmp_path)

    _run(_stream(_codex_permission(
        command="whoami > /tmp/ra-drill-probe.txt",
        description="Do you want to allow the exact command to write /tmp/ra-drill-probe.txt?",
    )), directory, provider="codex")

    record = _record(directory)
    assert record["detail"] == (
        "Do you want to allow the exact command to write /tmp/ra-drill-probe.txt? "
        "— $ whoami > /tmp/ra-drill-probe.txt"
    )
    assert record["ask"] == "Bash", "the class stays the headline; the words are beside it"


def test_a_codex_apply_patch_permission_request_carries_its_command_alone(
    tmp_path: Path,
) -> None:
    """`apply_patch` was measured carrying `command` and **no** `description`.

    So the rendered shape has to survive a missing half rather than assume both. This is the
    case that would have produced `"None — $ ..."` had the formatter been written from the
    `Bash` sample alone.
    """
    directory = _spool(tmp_path)

    _run(_stream(_codex_permission(
        "apply_patch",
        command="*** Begin Patch\n*** Update File: README.md\n@@\n drill\n+EDIT\n*** End Patch",
    )), directory, provider="codex")

    record = _record(directory)
    assert record["detail"] is not None
    assert record["detail"].startswith("$ *** Begin Patch")
    assert "—" not in record["detail"], "no dangling separator for the half that is absent"
    assert record["ask"] == "apply_patch"


def test_a_codex_permission_request_with_only_a_reason_renders_that_half_alone(
    tmp_path: Path,
) -> None:
    """The mirror of the case above, so the formatter is pinned from both sides."""
    directory = _spool(tmp_path)

    _run(_stream(_codex_permission(description="Allow writing in the working directory?")),
         directory, provider="codex")

    record = _record(directory)
    assert record["detail"] == "Allow writing in the working directory?"
    assert "$" not in record["detail"], "no empty command marker for the half that is absent"


def test_a_codex_permission_request_carrying_neither_key_is_still_a_record(
    tmp_path: Path,
) -> None:
    """Wordless, never dropped.

    A silent ask is the exact failure DEC-098 reverses, so a payload this parser cannot find
    words in must still arrive as a `needs_answer` the owner can act on -- with its class, and
    with `detail` absent rather than invented.
    """
    directory = _spool(tmp_path)

    _run(_stream(_codex_permission("Read")), directory, provider="codex")

    record = _record(directory)
    assert record["detail"] is None, "nothing invents words the payload did not carry"
    assert record["ask"] == "Read"
    assert record["event"] == "PermissionRequest"


def test_a_codex_permission_request_detail_is_bounded_to_one_line(tmp_path: Path) -> None:
    """`apply_patch`'s command is a multi-line patch envelope, not a one-line shell string.

    It is the first detail source this project has read that is *structurally* multi-line, so
    the bound is asserted on the shape that actually arrives rather than on a synthetic essay.
    """
    directory = _spool(tmp_path)

    _run(_stream(_codex_permission(
        "apply_patch",
        command="*** Begin Patch\n" + ("a" * 400) + "\n*** End Patch",
    )), directory, provider="codex")

    detail = _record(directory)["detail"]
    assert "\n" not in detail
    assert len(detail) <= MAXIMUM_DETAIL_CHARACTERS


def test_a_codex_permission_request_never_spools_the_layout_its_payload_carries(
    tmp_path: Path,
) -> None:
    """DEC-098 widened the licence by exactly two keys and no further.

    `transcript_path` and `cwd` are on this event and remain refused; the amendment note on the
    2026-08-29 acceptance document says so in the same words.
    """
    directory = _spool(tmp_path)

    _run(_stream(_codex_permission(command="ls", description="List files")),
         directory, provider="codex")

    written = json.dumps(_record(directory))
    assert "/home/user/.codex" not in written
    assert "/home/user/workspace" not in written


# --- Claude `PermissionRequest` (DEC-098, DEC-051) --------------------------------------------
#
# Shaped from real captures in `docs/acceptance-2026-09-19-ask-payloads.md`, taken against
# Claude Code v2.1.278. `Bash` carries the same two keys Codex's does -- which is why one reader
# serves both -- while `Edit` carries a path and `AskUserQuestion` carries a list.

_CLAUDE_PERMISSION_PAYLOAD: dict[str, Any] = {
    "session_id": "6b1c1f10-6d2f-4a3f-9a4e-0c2b8f4f1a22",
    "transcript_path": "/home/user/.claude/projects/infra/6b1c1f10.jsonl",
    "cwd": "/home/user/workspace",
    "prompt_id": "40cd6023-f3c1-4390-ae1b-316780d135a5",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {},
}


def _claude_permission(tool_name: str = "Bash", **tool_input: Any) -> dict[str, Any]:
    payload = {**_CLAUDE_PERMISSION_PAYLOAD, "tool_name": tool_name}
    payload["tool_input"] = dict(tool_input)
    return payload


def test_a_claude_bash_permission_request_says_what_it_is_asking(tmp_path: Path) -> None:
    """The same two keys Codex's `Bash` carries, read by the same reader.

    Claude's `needs_answer` was no better off than Codex's before this: the only detail it ever
    carried was the constant `Claude needs your permission`, 86 times since 2026-09-01.
    """
    directory = _spool(tmp_path)

    _run(_stream(_claude_permission(
        command='curl -s -o /dev/null -w "%{http_code}" https://example.com',
        description="Check HTTP status code for example.com",
    )), directory)

    record = _record(directory)
    assert record["detail"] == (
        "Check HTTP status code for example.com "
        '— $ curl -s -o /dev/null -w "%{http_code}" https://example.com'
    )
    assert record["ask"] == "Bash"


def test_a_claude_file_permission_request_names_the_file(tmp_path: Path) -> None:
    """`Edit` carries no command and no description -- it carries the path it wants to change.

    Its `old_string` and `new_string` carry file CONTENT and are deliberately not read: the
    owner is being asked which file, not shown a diff on their phone.
    """
    directory = _spool(tmp_path)

    _run(_stream(_claude_permission(
        "Edit",
        file_path="/home/user/workspace/README.md",
        old_string="a secret sentence from the file",
        new_string="another secret sentence",
        replace_all=False,
    )), directory)

    record = _record(directory)
    assert record["detail"] == "/home/user/workspace/README.md"
    assert record["ask"] == "Edit"
    written = json.dumps(record)
    assert "secret sentence" not in written, "file content is not what the ask is about"


def test_a_claude_question_permission_request_carries_the_question(tmp_path: Path) -> None:
    """`AskUserQuestion` nests its text one level deeper, inside a list.

    The question is the single most answerable thing this service can put on a phone, so the
    reader descends to it -- but only to the first question's text, never the options.
    """
    directory = _spool(tmp_path)

    _run(_stream(_claude_permission(
        "AskUserQuestion",
        questions=[{
            "question": "Do you prefer tabs or spaces for indentation?",
            "header": "Indentation",
            "options": [{"label": "Spaces", "description": "…"}],
            "multiSelect": False,
        }],
    )), directory)

    record = _record(directory)
    assert record["detail"] == "Do you prefer tabs or spaces for indentation?"
    assert record["ask"] == "AskUserQuestion"


def test_a_claude_permission_request_never_spools_the_layout_it_carries(tmp_path: Path) -> None:
    """DEC-098 admits the ask's words and nothing around them, on this provider too."""
    directory = _spool(tmp_path)

    _run(_stream(_claude_permission(command="ls", description="List files")), directory)

    written = json.dumps(_record(directory))
    assert "/home/user/.claude/projects" not in written
    assert "/home/user/workspace" not in written


def test_a_claude_permission_request_with_an_unreadable_tool_input_is_still_a_record(
    tmp_path: Path,
) -> None:
    """Wordless, never dropped -- the rule is the provider-independent one."""
    directory = _spool(tmp_path)

    payload = {**_CLAUDE_PERMISSION_PAYLOAD, "tool_name": "Read", "tool_input": "not a mapping"}
    _run(_stream(payload), directory)

    record = _record(directory)
    assert record["detail"] is None
    assert record["ask"] == "Read"
    assert record["event"] == "PermissionRequest"


# --- The "a turn started" marker (BL-108, DEC-104) ---------------------------------------------

_SUBMIT_PAYLOAD: dict[str, Any] = {
    "session_id": "f4020001-e712-4832-9fc8-dd28d38d5b8a",
    "transcript_path": "/home/user/.claude/projects/infra/f4020001.jsonl",
    "cwd": "/tmp/scratch",
    "permission_mode": "auto",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "count to ten",
}


def _marker(directory: Path, session_id: str = "s-42") -> Path:
    return directory / "turns" / session_id


def _records(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_submit_starts_a_marker_and_spools_no_record(provider: str, tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(_SUBMIT_PAYLOAD), directory, provider=provider) == 0

    assert _marker(directory).is_file()
    assert _marker(directory).read_bytes() == _SUBMIT_PAYLOAD["session_id"].encode()
    assert _records(directory) == []


@pytest.mark.parametrize(
    ("provider", "event"), [("claude", "Stop"), ("claude", "StopFailure"), ("codex", "Stop")]
)
def test_a_finished_turn_ends_the_marker_and_still_spools(
    provider: str, event: str, tmp_path: Path
) -> None:
    directory = _spool(tmp_path)
    _run(_stream(_SUBMIT_PAYLOAD), directory, provider=provider)

    finished = {**_STOP_PAYLOAD, "hook_event_name": event, "error": "rate_limit"}
    assert _run(_stream(finished), directory, provider=provider) == 0

    assert not _marker(directory).exists()
    assert len(_records(directory)) == 1


def test_an_event_that_neither_starts_nor_ends_a_turn_leaves_the_marker(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    _run(_stream(_SUBMIT_PAYLOAD), directory)

    notification = {**_STOP_PAYLOAD, "hook_event_name": "Notification"}
    _run(_stream(notification), directory)

    assert _marker(directory).is_file()


@pytest.mark.parametrize("session_id", [None, "../escape", "a b"])
def test_an_unmanaged_or_malformed_session_starts_no_marker(
    session_id: str | None, tmp_path: Path
) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(_SUBMIT_PAYLOAD), directory, session_id=session_id) == 0

    assert not (directory / "turns").exists()
    assert list(tmp_path.rglob("escape")) == []


def test_a_payload_that_is_not_json_starts_no_marker(tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    assert _run(io.BytesIO(b"UserPromptSubmit"), directory) == 0

    assert not (directory / "turns").exists()


class _Exploding:
    def start(self, session_id: str, owner: str | None = None) -> None:
        raise RuntimeError("disk on fire")

    def end(self, session_id: str) -> None:
        raise RuntimeError("disk on fire")

    def end_if_owned_by(self, session_id: str, owner: object) -> None:
        raise RuntimeError("disk on fire")


@pytest.mark.parametrize("payload", [_SUBMIT_PAYLOAD, _STOP_PAYLOAD])
def test_a_marker_that_cannot_be_written_never_fails_the_hook(
    payload: dict[str, Any], tmp_path: Path
) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(payload), directory, markers=_Exploding()) == 0


def test_opencode_never_starts_a_marker(tmp_path: Path) -> None:
    """Its "finished" is `session.idle`, which ends no marker, so one started there would stick."""
    directory = _spool(tmp_path)

    assert _run(_stream(_SUBMIT_PAYLOAD), directory, provider="opencode") == 0

    assert not (directory / "turns").exists()


def test_a_marker_that_cannot_be_ended_still_leaves_the_finished_record(tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(_STOP_PAYLOAD), directory, markers=_Exploding()) == 0

    assert len(_records(directory)) == 1


_LONG = "x" * (MAXIMUM_PAYLOAD_BYTES * 4)
_SENTINEL = "turn-marker-sentinel-5b1d"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_an_over_bound_submit_still_starts_its_marker_and_is_read_only_to_the_bound(
    provider: str, tmp_path: Path
) -> None:
    """A pasted prompt past the bound is the owner's turn all the same (DEC-104).

    Its record was never kept, and still is not; only its event name is recovered, from the
    bounded prefix, so the relay does not read that streaming turn as idle.
    """
    directory = _spool(tmp_path)
    stream = _stream({**_SUBMIT_PAYLOAD, "prompt": f"{_SENTINEL} {_LONG}"})

    assert _run(stream, directory, provider=provider) == 0

    assert _marker(directory).is_file()
    assert _records(directory) == []
    assert stream.tell() <= MAXIMUM_PAYLOAD_BYTES + 1
    assert not any(
        _SENTINEL.encode() in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )


def test_an_over_bound_stop_still_ends_its_marker_and_spools_nothing(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    _run(_stream(_SUBMIT_PAYLOAD), directory)

    assert _run(_stream({**_STOP_PAYLOAD, "last_assistant_message": _LONG}), directory) == 0

    assert not _marker(directory).exists()
    assert _records(directory) == []


def test_an_event_name_past_the_bound_is_not_guessed(tmp_path: Path) -> None:
    """When the event name comes after the long field, it is not in the prefix: no marker."""
    directory = _spool(tmp_path)
    late = {"session_id": "s", "prompt": _LONG, "hook_event_name": "UserPromptSubmit"}

    assert _run(_stream(late), directory) == 0

    assert not (directory / "turns").exists()


def test_an_event_name_quoted_inside_a_value_is_never_read_as_the_event(tmp_path: Path) -> None:
    """Only a top-level key counts: a prompt that spells the key and a value names no event."""
    directory = _spool(tmp_path)
    _run(_stream(_SUBMIT_PAYLOAD), directory)
    spoof = {
        "session_id": "s",
        "prompt": '{"hook_event_name": "Stop"} "hook_event_name": "Stop" ' + _LONG,
        "hook_event_name": "UserPromptSubmit",
    }

    assert _run(_stream(spoof), directory) == 0

    assert _marker(directory).is_file()


@pytest.mark.parametrize("provider", ["opencode", "some-later-provider"])
def test_only_claude_and_codex_start_a_marker(provider: str, tmp_path: Path) -> None:
    """DEC-104 scopes markers to the two agents whose `Stop` ends them; a provider added later
    starts none until someone decides it should."""
    directory = _spool(tmp_path)

    assert _run(_stream(_SUBMIT_PAYLOAD), directory, provider=provider) == 0

    assert not (directory / "turns").exists()


def _as(agent: str, payload: dict[str, Any]) -> io.BytesIO:
    return _stream({**payload, "session_id": agent})


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_nested_agents_stop_leaves_its_parents_marker(provider: str, tmp_path: Path) -> None:
    """A `claude -p` or `codex exec` run by a tool call inherits the pane's session id, so its
    hooks fire against the parent's marker. Its `Stop` must not end the parent's running turn."""
    directory = _spool(tmp_path)
    _run(_as("parent", _SUBMIT_PAYLOAD), directory, provider=provider)

    _run(_as("child", _SUBMIT_PAYLOAD), directory, provider=provider)
    _run(_as("child", _STOP_PAYLOAD), directory, provider=provider)
    assert _marker(directory).is_file()

    _run(_as("parent", _STOP_PAYLOAD), directory, provider=provider)
    assert not _marker(directory).exists()


def test_an_over_bound_stop_from_the_owner_still_ends_its_marker(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    _run(_as("parent", _SUBMIT_PAYLOAD), directory)

    _run(_as("child", {**_STOP_PAYLOAD, "last_assistant_message": _LONG}), directory)
    assert _marker(directory).is_file()
    _run(_as("parent", {**_STOP_PAYLOAD, "last_assistant_message": _LONG}), directory)

    assert not _marker(directory).exists()


def test_a_stop_whose_payload_names_no_agent_leaves_an_owned_marker(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    _run(_as("parent", _SUBMIT_PAYLOAD), directory)
    anonymous = {key: value for key, value in _STOP_PAYLOAD.items() if key != "session_id"}

    _run(_stream(anonymous), directory)

    assert _marker(directory).is_file()
