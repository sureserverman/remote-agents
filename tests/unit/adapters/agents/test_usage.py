"""Reading each provider's own working files for what a session has spent.

The fixtures below are trimmed copies of records observed on a real host on 2026-08-27, not
shapes invented to match the parser. That distinction is the reason DEC-013 clause (4) exists:
the activity spool and its classifier were once fixtured against each other and agreed
perfectly about a field the agent had never sent. Every field asserted here — `usage`'s four
token classes, `last_token_usage.total_tokens`, `model_context_window`, `rate_limits.primary`
— was read out of a live transcript, rollout or cache first.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.usage import (
    ClaudeUsageReader,
    CodexUsageReader,
    CursorUsageReader,
    OpenCodeUsageReader,
    ProfileUsageReaders,
)
from remote_agents.domain.models import ProfileId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.ports.agent_usage import UsageQuery

LAUNCHED_AT = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)


def _query(profile: str, workspace: Path, *, resume: str | None = None) -> UsageQuery:
    return UsageQuery(ProfileId(profile), workspace, LAUNCHED_AT, resume)


def _written(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _claude_turn(read: int, *, sidechain: bool = False) -> dict:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 759,
                "cache_read_input_tokens": read,
                "output_tokens": 787,
            },
        },
    }


def _in(**offset: int) -> int:
    """A reset instant in Unix seconds, the way a rollout writes one."""
    return int((datetime.now(UTC) + timedelta(**offset)).timestamp())


def _iso_in(**offset: int) -> str:
    """A reset instant as the `...Z` ISO string the status-line cache writes.

    Relative for the same reason `_in` is. The first version of these fixtures used the
    literals captured off the real cache — `2026-08-27T10:10:00Z` and `09:00:00Z` — and they
    passed all morning and began failing at 10:10, because the lapsed-window rule under test
    had by then correctly dropped the very windows the assertion wanted. A fixture whose
    outcome depends on the wall clock tests the clock.
    """
    return (
        (datetime.now(UTC) + timedelta(**offset)).replace(microsecond=0).isoformat()
    ).replace("+00:00", "Z")


def _codex_token_count(*, last: int, window: int, primary: float, secondary: float) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"total_tokens": last * 3},
                "last_token_usage": {"total_tokens": last},
                "model_context_window": window,
            },
            # Unix seconds, which is the shape a live rollout carries. The *values* are
            # relative to now rather than the captured literals, because a window whose reset
            # has passed is deliberately dropped (see the lapsed-window test below) and a
            # frozen literal would quietly turn every assertion here into that case instead.
            "rate_limits": {
                "primary": {
                    "used_percent": primary,
                    "window_minutes": 300,
                    "resets_at": _in(hours=4),
                },
                "secondary": {
                    "used_percent": secondary,
                    "window_minutes": 10080,
                    "resets_at": _in(days=5),
                },
            },
        },
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "dev" / "remote-agents"
    directory.mkdir(parents=True)
    return directory


# --- claude ------------------------------------------------------------------------------


def _claude_reader(tmp_path: Path, *, cache: Path | None = None) -> ClaudeUsageReader:
    return ClaudeUsageReader(
        sessions_root=tmp_path / "claude-projects",
        limits_cache_root=cache if cache is not None else tmp_path / "absent-cache",
    )


def _transcript_dir(tmp_path: Path, workspace: Path) -> Path:
    return tmp_path / "claude-projects" / str(workspace.resolve()).replace("/", "-")


def test_claude_context_is_the_sum_of_the_last_turns_four_token_classes(
    tmp_path: Path, workspace: Path
) -> None:
    """No single field is the context: `input_tokens` alone reads as 2 on a fully cached turn."""
    transcript = _written(
        _transcript_dir(tmp_path, workspace) / "11111111-1111-4111-8111-111111111111.jsonl",
        [_claude_turn(631_972)],
    )
    _touch(transcript, LAUNCHED_AT + timedelta(minutes=5))

    usage = _claude_reader(tmp_path).read(_query("claude", workspace))

    assert usage is not None
    assert usage.context is not None
    assert usage.context.used_tokens == 2 + 759 + 631_972 + 787


def test_claude_states_no_context_ceiling_so_no_percentage_is_invented(
    tmp_path: Path, workspace: Path
) -> None:
    """The transcript records what a turn used and never the window it used it out of."""
    transcript = _written(
        _transcript_dir(tmp_path, workspace) / "11111111-1111-4111-8111-111111111111.jsonl",
        [_claude_turn(1_000)],
    )
    _touch(transcript, LAUNCHED_AT + timedelta(minutes=5))

    usage = _claude_reader(tmp_path).read(_query("claude", workspace))

    assert usage is not None
    assert usage.context is not None
    assert usage.context.limit_tokens is None
    assert usage.context.used_fraction is None


def test_a_sidechain_turn_is_not_the_sessions_context(tmp_path: Path, workspace: Path) -> None:
    """A sub-agent's window is not the session's, and reporting one is unreconcilable."""
    transcript = _written(
        _transcript_dir(tmp_path, workspace) / "11111111-1111-4111-8111-111111111111.jsonl",
        [_claude_turn(500_000), _claude_turn(9_000, sidechain=True)],
    )
    _touch(transcript, LAUNCHED_AT + timedelta(minutes=5))

    usage = _claude_reader(tmp_path).read(_query("claude", workspace))

    assert usage is not None
    assert usage.context is not None
    assert usage.context.used_tokens == 2 + 759 + 500_000 + 787


def test_a_conversation_that_predates_the_session_is_never_attributed_to_it(
    tmp_path: Path, workspace: Path
) -> None:
    """Otherwise a freshly launched agent reports last week's context as its own."""
    stale = _written(
        _transcript_dir(tmp_path, workspace) / "22222222-2222-4222-8222-222222222222.jsonl",
        [_claude_turn(999_000)],
    )
    _touch(stale, LAUNCHED_AT - timedelta(days=7))

    assert _claude_reader(tmp_path).read(_query("claude", workspace)) is None


def test_a_resumed_session_names_its_transcript_and_no_search_happens(
    tmp_path: Path, workspace: Path
) -> None:
    """The resumed conversation keeps its filename, so the newest file must not win over it."""
    directory = _transcript_dir(tmp_path, workspace)
    resumed = _written(directory / "33333333-3333-4333-8333-333333333333.jsonl", [_claude_turn(11)])
    newer = _written(directory / "44444444-4444-4444-8444-444444444444.jsonl", [_claude_turn(88)])
    _touch(resumed, LAUNCHED_AT + timedelta(minutes=1))
    _touch(newer, LAUNCHED_AT + timedelta(minutes=9))

    usage = _claude_reader(tmp_path).read(
        _query("claude", workspace, resume="33333333-3333-4333-8333-333333333333")
    )

    assert usage is not None
    assert usage.context is not None
    assert usage.context.used_tokens == 2 + 759 + 11 + 787


def test_claude_limits_come_from_the_cache_and_say_that_they_did(
    tmp_path: Path, workspace: Path
) -> None:
    """Borrowed from a file this project does not own, so it is never shown as our measurement."""
    cache = tmp_path / "claude-cache"
    cache.mkdir()
    _written_json(
        cache / "statusline-usage-cache-d1c0b541.json",
        {
            "five_hour": {"utilization": 2, "resets_at": _iso_in(hours=3)},
            "seven_day": {"utilization": 88, "resets_at": _iso_in(days=2)},
        },
    )
    transcript = _written(
        _transcript_dir(tmp_path, workspace) / "11111111-1111-4111-8111-111111111111.jsonl",
        [_claude_turn(1_000)],
    )
    _touch(transcript, LAUNCHED_AT + timedelta(minutes=5))

    usage = _claude_reader(tmp_path, cache=cache).read(_query("claude", workspace))

    assert usage is not None
    assert usage.stale_source == "status-line cache"
    assert [(window.label, window.used_percent) for window in usage.windows] == [
        ("5h", 2.0),
        ("week", 88.0),
    ]


def test_a_stale_cache_is_discarded_rather_than_shown(tmp_path: Path, workspace: Path) -> None:
    """A rate-limit window moves on its own, so an old copy may describe a window that reset."""
    cache = tmp_path / "claude-cache"
    cache.mkdir()
    document = _written_json(
        cache / "statusline-usage-cache-d1c0b541.json",
        # A *future* reset on purpose: this test is about the cache file's age, and a lapsed
        # reset would empty the windows for the other reason and let the assertion pass
        # without exercising the staleness rule at all.
        {"five_hour": {"utilization": 2, "resets_at": _iso_in(hours=3)}},
    )
    _touch(document, datetime.now(UTC) - timedelta(hours=6))
    transcript = _written(
        _transcript_dir(tmp_path, workspace) / "11111111-1111-4111-8111-111111111111.jsonl",
        [_claude_turn(1_000)],
    )
    _touch(transcript, LAUNCHED_AT + timedelta(minutes=5))

    usage = _claude_reader(tmp_path, cache=cache).read(_query("claude", workspace))

    assert usage is not None
    assert usage.windows == ()
    assert usage.stale_source is None


# --- codex -------------------------------------------------------------------------------


def _rollout(tmp_path: Path, workspace: Path, name: str, records: list[dict]) -> Path:
    day = LAUNCHED_AT
    path = (
        tmp_path
        / "codex-sessions"
        / f"{day:%Y}"
        / f"{day:%m}"
        / f"{day:%d}"
        / f"rollout-2026-08-27T06-05-00-{name}.jsonl"
    )
    meta = {"type": "session_meta", "payload": {"cwd": str(workspace.resolve())}}
    written = _written(path, [meta, *records])
    _touch(written, LAUNCHED_AT + timedelta(minutes=10))
    return written


def test_codex_reports_its_context_against_the_window_it_states(
    tmp_path: Path, workspace: Path
) -> None:
    _rollout(
        tmp_path,
        workspace,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [_codex_token_count(last=24_349, window=258_400, primary=2.0, secondary=0.0)],
    )

    usage = CodexUsageReader(sessions_root=tmp_path / "codex-sessions").read(
        _query("codex", workspace)
    )

    assert usage is not None
    assert usage.context is not None
    assert usage.context.used_tokens == 24_349
    assert usage.context.limit_tokens == 258_400
    assert round(usage.context.used_fraction * 100) == 9


def test_codex_context_is_the_last_turn_and_not_the_running_total(
    tmp_path: Path, workspace: Path
) -> None:
    """`total_token_usage` accumulates and passes the window within a few turns."""
    _rollout(
        tmp_path,
        workspace,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [_codex_token_count(last=24_349, window=258_400, primary=2.0, secondary=0.0)],
    )

    usage = CodexUsageReader(sessions_root=tmp_path / "codex-sessions").read(
        _query("codex", workspace)
    )

    assert usage is not None
    assert usage.context is not None
    assert usage.context.used_tokens != 24_349 * 3


def test_codex_windows_are_named_by_the_duration_the_provider_states(
    tmp_path: Path, workspace: Path
) -> None:
    """`primary` and `secondary` are positions, not durations; `window_minutes` is the fact."""
    _rollout(
        tmp_path,
        workspace,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [_codex_token_count(last=100, window=258_400, primary=2.0, secondary=41.0)],
    )

    usage = CodexUsageReader(sessions_root=tmp_path / "codex-sessions").read(
        _query("codex", workspace)
    )

    assert usage is not None
    assert [(window.label, window.used_percent) for window in usage.windows] == [
        ("5h", 2.0),
        ("week", 41.0),
    ]
    assert all(window.resets_at is not None for window in usage.windows)


def test_a_rollout_from_another_workspace_is_never_matched(
    tmp_path: Path, workspace: Path
) -> None:
    """The workspace is matched exactly, so one project's usage cannot land on another's."""
    other = tmp_path / "dev" / "elsewhere"
    other.mkdir(parents=True)
    _rollout(
        tmp_path,
        other,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [_codex_token_count(last=99_999, window=258_400, primary=90.0, secondary=90.0)],
    )

    assert (
        CodexUsageReader(sessions_root=tmp_path / "codex-sessions").read(
            _query("codex", workspace)
        )
        is None
    )


def test_a_rollout_with_no_accounting_record_yet_is_matched_but_empty(
    tmp_path: Path, workspace: Path
) -> None:
    """Matched-and-silent is a different answer from unmatched, and the owner reads both."""
    _rollout(tmp_path, workspace, "aaaaaaaa-0000-4000-8000-000000000000", [])

    usage = CodexUsageReader(sessions_root=tmp_path / "codex-sessions").read(
        _query("codex", workspace)
    )

    assert usage is not None
    assert usage.is_empty


def test_a_window_that_has_already_reset_is_dropped_rather_than_reported(
    tmp_path: Path, workspace: Path
) -> None:
    """Observed on a live host: a RUNNING codex session idle since its last turn five days back.

    Codex stamps `rate_limits` onto the `token_count` event of a turn and never rewrites it, so
    an idle session keeps serving the percentages from whenever it last spoke. The context from
    that record is still right — a conversation that took no turns did not grow — but the
    *window* the percentage counted against has since closed and reopened, and rendering it
    read `week 43% (resets in 0m)`.
    """
    lapsed = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"total_tokens": 194_000},
                "model_context_window": 258_400,
            },
            "rate_limits": {
                "primary": {
                    "used_percent": 43.0,
                    "window_minutes": 10080,
                    "resets_at": 1787341653,
                },
                "secondary": None,
            },
        },
    }
    _rollout(tmp_path, workspace, "aaaaaaaa-0000-4000-8000-000000000000", [lapsed])

    usage = CodexUsageReader(sessions_root=tmp_path / "codex-sessions").read(
        _query("codex", workspace)
    )

    assert usage is not None
    assert usage.windows == ()
    assert usage.context is not None
    assert usage.context.used_tokens == 194_000


def test_a_null_secondary_window_is_skipped_rather_than_mislabelled(
    tmp_path: Path, workspace: Path
) -> None:
    """A live rollout carried `primary` at 10080 minutes and `secondary: null`.

    Labelling by position would have called that weekly window `5h`, which is the whole reason
    `_window_label` reads `window_minutes` instead.
    """
    record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"total_tokens": 100}},
            "rate_limits": {
                "primary": {
                    "used_percent": 43.0,
                    "window_minutes": 10080,
                    "resets_at": _in(days=2),
                },
                "secondary": None,
            },
        },
    }
    _rollout(tmp_path, workspace, "aaaaaaaa-0000-4000-8000-000000000000", [record])

    usage = CodexUsageReader(sessions_root=tmp_path / "codex-sessions").read(
        _query("codex", workspace)
    )

    assert usage is not None
    assert [window.label for window in usage.windows] == ["week"]


# --- opencode ----------------------------------------------------------------------------


def _opencode_database(tmp_path: Path, workspace: Path, tokens: dict) -> Path:
    path = tmp_path / "opencode.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL);"
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
        " time_created INTEGER NOT NULL, data TEXT NOT NULL);"
    )
    connection.execute("INSERT INTO session VALUES (?, ?)", ("ses_1", str(workspace.resolve())))
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        (
            "msg_1",
            "ses_1",
            int((LAUNCHED_AT + timedelta(minutes=3)).timestamp() * 1000),
            json.dumps({"role": "assistant", "tokens": tokens}),
        ),
    )
    connection.commit()
    connection.close()
    return path


def test_opencode_context_is_the_total_its_own_accounting_gives(
    tmp_path: Path, workspace: Path
) -> None:
    database = _opencode_database(
        tmp_path,
        workspace,
        {"total": 12_952, "input": 2_191, "output": 9, "cache": {"read": 10_752, "write": 0}},
    )

    usage = OpenCodeUsageReader(database=database).read(_query("opencode", workspace))

    assert usage is not None
    assert usage.context is not None
    assert usage.context.used_tokens == 12_952


def test_opencode_publishes_no_rate_limits_and_claims_none(
    tmp_path: Path, workspace: Path
) -> None:
    database = _opencode_database(tmp_path, workspace, {"total": 100})

    usage = OpenCodeUsageReader(database=database).read(_query("opencode", workspace))

    assert usage is not None
    assert usage.windows == ()


# --- cursor ------------------------------------------------------------------------------


def test_cursor_answers_that_it_publishes_nothing_rather_than_failing_to_answer(
    workspace: Path,
) -> None:
    """Its chat store holds the conversation and no accounting of any kind.

    Empty rather than `None` on purpose: `None` renders as "no conversation matched yet", which
    invites the owner to wait for a number cursor-agent is never going to write down.
    """
    usage = CursorUsageReader().read(_query("cursor-agent", workspace))

    assert usage.is_empty
    assert usage.context is None


# --- dispatch ----------------------------------------------------------------------------


def test_an_unknown_profile_answers_nothing_rather_than_raising(workspace: Path) -> None:
    assert ProfileUsageReaders(readers=()).read(_query("claude", workspace)) is None


def test_a_reader_that_fails_costs_one_usage_line_and_nothing_else(workspace: Path) -> None:
    """This decorates a screen whose real content is a session's state and its stop actions."""

    class Exploding:
        profiles = frozenset({ProfileId("claude")})

        def read(self, query: UsageQuery) -> None:
            raise OSError("the provider changed its layout under an upgrade")

    assert ProfileUsageReaders(readers=(Exploding(),)).read(_query("claude", workspace)) is None


def test_the_default_reader_set_covers_every_curated_profile() -> None:
    """Read from the closed profile set rather than a list here, so a sixth profile fails this.

    A curated profile with no reader answers `None` forever, and the bot renders that as "no
    conversation matched yet" — which tells the owner to wait for a number nothing will ever
    produce. Restating the five names here would make this test agree with itself instead.
    """
    covered = ProfileUsageReaders().profiles

    assert {definition.profile_id for definition in closed_profiles()} <= covered


def _written_json(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _touch(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))
