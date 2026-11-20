"""Claude's reader prefers the figure Claude itself reported, while it is fresh (BL-118).

The status-line hop keeps `{"used_percentage", "context_window_size", "recorded_at"}` per
managed session under `<state>/claude-context/<id>.json` (DEC-146). The reader uses it when it
is under ten minutes old and well formed, and the transcript estimate otherwise; a record is
removed once its session has ended.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.claude.usage import ClaudeUsageReader
from remote_agents.adapters.agents.registry import ProfileUsageReaders
from remote_agents.application.session_views import context_gauge, session_row
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import ContextWindow, UsageQuery

LAUNCHED_AT = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
SESSION = "0eaa3b3e-d567-4349-9809-d1b04e1ce23b"
TRANSCRIPT_TOKENS = 2 + 759 + 631_972 + 787


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def _transcript(tmp_path: Path, workspace: Path) -> Path:
    name = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(workspace.resolve()))
    path = tmp_path / "claude-projects" / name / "11111111-1111-4111-8111-111111111111.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    turn = {
        "type": "assistant",
        "isSidechain": False,
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 759,
                "cache_read_input_tokens": 631_972,
                "output_tokens": 787,
            },
        },
    }
    path.write_text(json.dumps(turn) + "\n", encoding="utf-8")
    moment = (LAUNCHED_AT + timedelta(minutes=5)).timestamp()
    os.utime(path, (moment, moment))
    return path


def _record(
    directory: Path,
    *,
    age: timedelta = timedelta(minutes=2),
    percentage: object = 41.5,
    size: object = 1_000_000,
    session: str = SESSION,
) -> Path:
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{session}.json"
    path.write_text(
        json.dumps(
            {
                "used_percentage": percentage,
                "context_window_size": size,
                "recorded_at": (NOW - age).timestamp(),
            }
        ),
        encoding="utf-8",
    )
    return path


def _reader(tmp_path: Path) -> ClaudeUsageReader:
    return ClaudeUsageReader(
        sessions_root=tmp_path / "claude-projects",
        limits_path=tmp_path / "absent-limits.json",
        context_window=1_000_000,
        context_window_stated=True,
        context_directory=tmp_path / "claude-context",
        now=lambda: NOW,
    )


def _query(workspace: Path, session: str | None = SESSION) -> UsageQuery:
    return UsageQuery(ProfileId("claude"), workspace, LAUNCHED_AT, None, session_id=session)


def _context(tmp_path: Path, workspace: Path, session: str | None = SESSION) -> ContextWindow:
    usage = _reader(tmp_path).read(_query(workspace, session))
    assert usage is not None and usage.context is not None
    return usage.context


# --- which figure -----------------------------------------------------------------------------


def test_a_fresh_record_gives_claude_s_own_figure_with_no_declared_hedge(
    tmp_path: Path, workspace: Path
) -> None:
    _transcript(tmp_path, workspace)
    _record(tmp_path / "claude-context")

    context = _context(tmp_path, workspace)

    assert context.provider_reported
    assert context.reported_percent == 41.5
    assert context.limit_tokens == 1_000_000
    assert not context.limit_declared
    # One rounding rule for every percentage (`whole_percent`): 41.5 rounds to the even 42.
    assert context_gauge(context).endswith(" 42%")


def test_a_record_eleven_minutes_old_gives_the_transcript_estimate_with_its_hedge(
    tmp_path: Path, workspace: Path
) -> None:
    _transcript(tmp_path, workspace)
    _record(tmp_path / "claude-context", age=timedelta(minutes=11))

    context = _context(tmp_path, workspace)

    assert not context.provider_reported
    assert context.used_tokens == TRANSCRIPT_TOKENS
    assert context.limit_declared


def test_a_record_dated_in_the_future_is_a_wrong_clock_not_a_reading(
    tmp_path: Path, workspace: Path
) -> None:
    _transcript(tmp_path, workspace)
    _record(tmp_path / "claude-context", age=-timedelta(minutes=5))

    assert not _context(tmp_path, workspace).provider_reported


def test_a_fresh_record_answers_before_the_transcript_has_a_turn(
    tmp_path: Path, workspace: Path
) -> None:
    _record(tmp_path / "claude-context", percentage=3)

    context = _context(tmp_path, workspace)

    assert context.reported_percent == 3


def test_a_query_with_no_session_id_reads_the_transcript(tmp_path: Path, workspace: Path) -> None:
    _transcript(tmp_path, workspace)
    _record(tmp_path / "claude-context")

    assert not _context(tmp_path, workspace, session=None).provider_reported


def test_another_session_s_record_is_never_this_session_s(tmp_path: Path, workspace: Path) -> None:
    _transcript(tmp_path, workspace)
    _record(tmp_path / "claude-context", session="another-session")

    assert not _context(tmp_path, workspace).provider_reported


def test_a_reader_built_without_a_context_directory_reads_the_transcript(
    tmp_path: Path, workspace: Path
) -> None:
    _transcript(tmp_path, workspace)
    _record(tmp_path / "claude-context")
    reader = ClaudeUsageReader(
        sessions_root=tmp_path / "claude-projects",
        limits_path=tmp_path / "absent-limits.json",
        now=lambda: NOW,
    )

    usage = reader.read(_query(workspace))

    assert usage is not None and usage.context is not None
    assert not usage.context.provider_reported


# --- what is not a record ---------------------------------------------------------------------


def test_a_record_that_is_a_link_is_ignored(tmp_path: Path, workspace: Path) -> None:
    _transcript(tmp_path, workspace)
    real = _record(tmp_path / "elsewhere")
    directory = tmp_path / "claude-context"
    directory.mkdir()
    (directory / f"{SESSION}.json").symlink_to(real)

    assert not _context(tmp_path, workspace).provider_reported


def test_a_context_directory_that_is_a_link_is_ignored(tmp_path: Path, workspace: Path) -> None:
    _transcript(tmp_path, workspace)
    _record(tmp_path / "elsewhere")
    (tmp_path / "claude-context").symlink_to(tmp_path / "elsewhere")

    assert not _context(tmp_path, workspace).provider_reported


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_record_is_ignored(tmp_path: Path, workspace: Path) -> None:
    _transcript(tmp_path, workspace)
    path = _record(tmp_path / "claude-context")
    path.chmod(0)
    try:
        assert not _context(tmp_path, workspace).provider_reported
    finally:
        path.chmod(0o600)


def test_a_record_over_four_kib_is_ignored(tmp_path: Path, workspace: Path) -> None:
    _transcript(tmp_path, workspace)
    path = _record(tmp_path / "claude-context")
    document = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**document, "padding": "x" * 4096}), encoding="utf-8")

    assert not _context(tmp_path, workspace).provider_reported


@pytest.mark.parametrize(
    ("percentage", "size"),
    [
        (None, 1_000_000),
        ("41.5", 1_000_000),
        (True, 1_000_000),
        (101, 1_000_000),
        (-1, 1_000_000),
        (41.5, None),
        (41.5, 0),
        (41.5, "1000000"),
    ],
    ids=["null", "string", "bool", "over-100", "negative", "no-size", "zero-size", "text-size"],
)
def test_a_malformed_record_is_ignored(
    tmp_path: Path, workspace: Path, percentage: object, size: object
) -> None:
    _transcript(tmp_path, workspace)
    _record(tmp_path / "claude-context", percentage=percentage, size=size)

    assert not _context(tmp_path, workspace).provider_reported


def test_a_record_that_is_not_json_is_ignored(tmp_path: Path, workspace: Path) -> None:
    _transcript(tmp_path, workspace)
    path = _record(tmp_path / "claude-context")
    path.write_bytes(b"{not json")

    assert not _context(tmp_path, workspace).provider_reported


# --- the row ----------------------------------------------------------------------------------


def test_the_row_draws_claude_s_own_figure_and_the_detail_does_not_say_declared() -> None:
    from remote_agents.application.session_views import _context_phrase

    reported = ContextWindow(415_000, 1_000_000, limit_declared=False, reported_percent=41.5)
    estimated = ContextWindow(415_000, 1_000_000, limit_declared=True)

    assert context_gauge(reported).endswith(" 42%")
    assert "declared" not in _context_phrase(reported)
    assert _context_phrase(estimated).endswith(" declared")
    assert session_row is not None


# --- ending a session removes its record ------------------------------------------------------


def test_forget_except_removes_every_record_but_the_kept_sessions(tmp_path: Path) -> None:
    directory = tmp_path / "claude-context"
    kept = _record(directory, session="live-one")
    ended = _record(directory, session="ended-one")
    stray = directory / "notes.txt"
    stray.write_text("not a record", encoding="utf-8")
    temporary = directory / ".live-one.json.cafef00d0000.tmp"
    temporary.write_text("{", encoding="utf-8")

    _reader(tmp_path).forget_except(frozenset({"live-one"}))

    assert kept.exists()
    assert not ended.exists()
    # Only `<id>.json` is this reader's to remove; the hop's temporaries are the hop's.
    assert stray.exists()
    assert temporary.exists()


def test_forget_except_never_follows_a_linked_directory(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    victim = _record(elsewhere, session="ended-one")
    (tmp_path / "claude-context").symlink_to(elsewhere)

    _reader(tmp_path).forget_except(frozenset())

    assert victim.exists()


def test_forget_except_is_quiet_with_no_directory(tmp_path: Path) -> None:
    _reader(tmp_path).forget_except(frozenset())
    ClaudeUsageReader(sessions_root=tmp_path).forget_except(frozenset())


def test_the_reader_set_forwards_forget_except_to_readers_that_keep_records(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "claude-context"
    ended = _record(directory, session="ended-one")

    class _NoRecords:
        profiles = frozenset({ProfileId("codex")})

    readers = ProfileUsageReaders([_reader(tmp_path), _NoRecords()])
    readers.forget_except(frozenset({"live-one"}))

    assert not ended.exists()


def test_the_production_descriptor_forwards_forget_except_through_the_limits_switch(
    tmp_path: Path,
) -> None:
    """With a switch, the registered capability wraps the hop reader; the sweep must reach it."""
    from remote_agents.adapters.agents.registry import provider_descriptors, usage_readers

    directory = tmp_path / "claude-context"
    ended = _record(directory, session="ended-one")
    kept = _record(directory, session="live-one")
    descriptors = provider_descriptors(
        claude_limits_path=tmp_path / "absent-limits.json",
        claude_context_directory=directory,
        claude_limits_switch=lambda: "status-line",
        claude_home=tmp_path,
    )

    usage_readers(descriptors).forget_except(frozenset({"live-one"}))

    assert not ended.exists()
    assert kept.exists()


def test_the_production_descriptor_reads_the_record_through_the_limits_switch(
    tmp_path: Path, workspace: Path
) -> None:
    from remote_agents.adapters.agents.registry import provider_descriptors, usage_readers

    directory = tmp_path / "claude-context"
    directory.mkdir(mode=0o700)
    (directory / f"{SESSION}.json").write_text(
        json.dumps(
            {
                "used_percentage": 41.5,
                "context_window_size": 1_000_000,
                "recorded_at": datetime.now(UTC).timestamp() - 30,
            }
        ),
        encoding="utf-8",
    )
    descriptors = provider_descriptors(
        claude_limits_path=tmp_path / "absent-limits.json",
        claude_context_directory=directory,
        claude_limits_switch=lambda: "status-line",
        claude_home=tmp_path,
    )
    query = UsageQuery(ProfileId("claude"), workspace, LAUNCHED_AT, None, session_id=SESSION)

    usage = usage_readers(descriptors).read(query)

    assert usage is not None and usage.context is not None
    assert usage.context.reported_percent == 41.5
