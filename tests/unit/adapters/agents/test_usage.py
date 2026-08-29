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
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.usage import (
    _ACCOUNT_ROLLOUT_DAYS,
    ClaudeUsageReader,
    CodexUsageReader,
    CursorUsageReader,
    OpenCodeUsageReader,
    ProfileUsageReaders,
)
from remote_agents.domain.models import ProfileId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.ports.agent_usage import AgentLimits, UsageQuery, UsageWindow

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
    return ((datetime.now(UTC) + timedelta(**offset)).replace(microsecond=0).isoformat()).replace(
        "+00:00", "Z"
    )


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


def test_a_rollout_from_another_workspace_is_never_matched(tmp_path: Path, workspace: Path) -> None:
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
        CodexUsageReader(sessions_root=tmp_path / "codex-sessions").read(_query("codex", workspace))
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


def test_opencode_publishes_no_rate_limits_and_claims_none(tmp_path: Path, workspace: Path) -> None:
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


# --- the account-wide limits read --------------------------------------------------------


def _account_rollout(
    tmp_path: Path,
    cwd: Path,
    name: str,
    records: list[dict],
    *,
    at,
    started: datetime | None = None,
) -> Path:
    """A rollout, with its start day and its last-write time chosen independently.

    `started` picks the **day directory**, which Codex keys to when the session began; `at`
    picks the **mtime**, which is when it last wrote. Defaulting `started` to `LAUNCHED_AT`
    keeps the common case short, but the two being separate parameters is the point: the
    first version of this helper derived the directory from `LAUNCHED_AT` unconditionally, so
    every fixture had directory-day == write-day and could not express a long-running session
    — which is exactly the shape that broke the account read on a real host.
    """
    day = LAUNCHED_AT if started is None else started
    path = (
        tmp_path
        / "codex-sessions"
        / f"{day:%Y}"
        / f"{day:%m}"
        / f"{day:%d}"
        / f"rollout-{day:%Y-%m-%dT%H-%M-%S}-{name}.jsonl"
    )
    meta = {"type": "session_meta", "payload": {"cwd": str(cwd.resolve())}}
    written = _written(path, [meta, *records])
    _touch(written, at)
    return written


def test_claude_account_limits_are_read_without_naming_a_session(tmp_path: Path) -> None:
    """The whole ask: the windows are the account's, so obtaining them takes no `UsageQuery`.

    `_limits()` already read the cache session-free; it was reachable only through `read()`,
    which needs a session to name. Nothing about the numbers changes — only who may ask.
    """
    cache = tmp_path / "claude-cache"
    cache.mkdir()
    _written_json(
        cache / "statusline-usage-cache-d1c0b541.json",
        {
            "five_hour": {"utilization": 2, "resets_at": _iso_in(hours=3)},
            "seven_day": {"utilization": 88, "resets_at": _iso_in(days=2)},
        },
    )

    limits = _claude_reader(tmp_path, cache=cache).limits()

    assert limits.profile_id == ProfileId("claude")
    assert [(window.label, window.used_percent) for window in limits.windows] == [
        ("5h", 2.0),
        ("week", 88.0),
    ]
    assert limits.stale_source == "status-line cache"


def test_a_stale_cache_leaves_the_account_block_empty_rather_than_wrong(tmp_path: Path) -> None:
    """One minute past the bound, so the boundary is asserted rather than the region past it."""
    cache = tmp_path / "claude-cache"
    cache.mkdir()
    document = _written_json(
        cache / "statusline-usage-cache-d1c0b541.json",
        {"five_hour": {"utilization": 2, "resets_at": _iso_in(hours=3)}},
    )
    _touch(document, datetime.now(UTC) - timedelta(minutes=31))

    limits = _claude_reader(tmp_path, cache=cache).limits()

    assert limits.windows == ()
    assert limits.stale_source is None


def test_codex_account_limits_come_from_the_newest_rollout_whatever_wrote_it(
    tmp_path: Path, workspace: Path
) -> None:
    """The account figure is in every rollout, so the newest one is the current one.

    The newer rollout is deliberately filed under a *different* workspace: a rate-limit
    window belongs to the plan rather than to a project, so an account read that filtered by
    workspace the way `read()` does would answer with the stale copy here.
    """
    elsewhere = tmp_path / "dev" / "other-project"
    elsewhere.mkdir(parents=True)
    _account_rollout(
        tmp_path,
        workspace,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [_codex_token_count(last=1_000, window=258_400, primary=10.0, secondary=1.0)],
        at=LAUNCHED_AT + timedelta(minutes=5),
    )
    _account_rollout(
        tmp_path,
        elsewhere,
        "bbbbbbbb-0000-4000-8000-000000000000",
        [_codex_token_count(last=2_000, window=258_400, primary=77.0, secondary=3.0)],
        at=LAUNCHED_AT + timedelta(minutes=40),
    )

    reader = CodexUsageReader(sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT)
    limits = reader.limits()

    assert limits.profile_id == ProfileId("codex")
    assert [window.used_percent for window in limits.windows] == [77.0, 3.0]
    assert limits.stale_source is None


def test_a_lapsed_account_window_is_dropped_exactly_as_a_session_one_is(
    tmp_path: Path, workspace: Path
) -> None:
    """An idle rollout keeps serving the percentages from whenever it last spoke (DEC-061)."""
    lapsed = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"total_tokens": 1}, "model_context_window": 258_400},
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
    _account_rollout(
        tmp_path,
        workspace,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [lapsed],
        at=LAUNCHED_AT + timedelta(minutes=5),
    )

    assert (
        CodexUsageReader(sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT)
        .limits()
        .windows
        == ()
    )


def test_the_providers_that_publish_no_limits_say_so_rather_than_failing(tmp_path: Path) -> None:
    """ "Not reported by this agent" is the honest render; an absent entry would be a gap."""
    assert OpenCodeUsageReader(database=tmp_path / "absent.db").limits().windows == ()
    assert CursorUsageReader().limits().windows == ()
    assert CursorUsageReader().limits().profile_id == ProfileId("cursor-agent")


def test_the_claude_variants_share_one_account_and_so_one_entry(tmp_path: Path) -> None:
    """`claude` and `claude-remote` are the same executable under a different argv.

    `domain/profiles.py` curates both to the `claude` binary, differing only by
    `--remote-control`, so they draw on one plan and one pair of rate-limit windows. An entry
    each would render the same account twice and read as two budgets.
    """
    cache = tmp_path / "claude-cache"
    cache.mkdir()
    _written_json(
        cache / "statusline-usage-cache-d1c0b541.json",
        {"five_hour": {"utilization": 2, "resets_at": _iso_in(hours=3)}},
    )
    readers = ProfileUsageReaders(
        (
            _claude_reader(tmp_path, cache=cache),
            CodexUsageReader(sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT),
            OpenCodeUsageReader(database=tmp_path / "absent.db"),
            CursorUsageReader(),
        )
    )

    named = [str(limits.profile_id) for limits in readers.limits()]

    assert named == ["claude", "codex", "opencode", "cursor-agent"]
    assert "claude-remote" not in named


def test_an_unreadable_source_costs_one_entry_and_never_the_screen(tmp_path: Path) -> None:
    """Total by construction, exactly as `read()` is and for the same reason."""

    class _Exploding:
        profiles = frozenset({ProfileId("codex")})
        limits_profile = ProfileId("codex")

        def read(self, query: UsageQuery) -> None:  # noqa: ARG002 - never reached here
            return None

        def limits(self) -> AgentLimits:
            raise OSError("the rollout directory went away mid-read")

    entries = ProfileUsageReaders((_Exploding(),)).limits()

    assert [str(entry.profile_id) for entry in entries] == ["codex"]
    assert entries[0].windows == ()


# --- the account-wide limits type --------------------------------------------------------


def test_agent_limits_names_the_account_it_answers_for() -> None:
    """The profile is on the type because the answer is the agent's, not a session's.

    `AgentUsage` needs no such field: it is handed back to the caller that named a session,
    so the identity is already in the caller's hand. A limits read takes no query at all, so
    a set of them would otherwise be a tuple of unlabelled percentages.
    """
    limits = AgentLimits(
        ProfileId("claude"),
        (UsageWindow("5h", 2.0), UsageWindow("week", 88.0)),
        stale_source="status-line cache",
    )

    assert limits.profile_id == ProfileId("claude")
    assert [window.label for window in limits.windows] == ["5h", "week"]
    assert limits.stale_source == "status-line cache"


def test_agent_limits_reuses_the_window_type_rather_than_restating_it() -> None:
    """One window type for both reads, so a session line and an account line cannot drift."""
    window = UsageWindow("5h", 2.0, resets_at=datetime.now(UTC) + timedelta(hours=3))

    assert AgentLimits(ProfileId("codex"), (window,)).windows[0] is window


def test_agent_limits_is_frozen_like_every_other_reading() -> None:
    limits = AgentLimits(ProfileId("codex"), ())

    with pytest.raises(FrozenInstanceError):
        limits.stale_source = "invented"  # type: ignore[misc]


def test_an_account_that_published_no_windows_is_representable() -> None:
    """DEC-061: absent is a first-class answer. `opencode` and `cursor-agent` are always this.

    Refusing an empty tuple here would push every reader that publishes nothing into either
    returning `None` — which this project words as "could not match", a different claim — or
    inventing a window. Both are the failure the decision names.
    """
    limits = AgentLimits(ProfileId("cursor-agent"), ())

    assert limits.windows == ()
    assert limits.stale_source is None


def test_a_borrowed_stamp_is_optional_because_only_one_reader_borrows() -> None:
    """Claude's limits come from a file this project does not own; Codex's come from its own."""
    assert AgentLimits(ProfileId("codex"), (UsageWindow("5h", 1.0),)).stale_source is None


def _written_json(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _touch(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


def test_a_long_running_session_is_still_the_newest_write(tmp_path: Path, workspace: Path) -> None:
    """The defect this test was written for, measured on a real host on 2026-08-29.

    Codex files a rollout under the day the session *started* and keeps appending to it for as
    long as the session lives. The account read asks a question about *write recency*, so
    filtering candidates by day-directory answers with the newest file among recently-*started*
    sessions — not the newest file. On the host this was found on, the genuinely newest rollout
    (written 19:55Z) sat in the 08/27 directory because that session began two days earlier, and
    the read returned `week 54%` from a stale file while the truth on disk was `5h 34%` and
    `week 61%`. A live window was omitted and the weekly figure was seven points low, with
    nothing marking it as old — a confidently wrong number, which is the one outcome DEC-061
    rules out entirely.

    25 of the 289 rollouts on that host (8.7%) had last been written on a day other than their
    directory's, with lags reaching eight days, so this is the ordinary case for a project whose
    whole purpose is long-lived managed agent sessions.
    """
    _account_rollout(
        tmp_path,
        workspace,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [_codex_token_count(last=1_000, window=258_400, primary=87.0, secondary=54.0)],
        at=LAUNCHED_AT + timedelta(hours=1),
        started=LAUNCHED_AT,
    )
    # Started two days earlier, still being written an hour after the other one stopped.
    _account_rollout(
        tmp_path,
        workspace,
        "bbbbbbbb-0000-4000-8000-000000000000",
        [_codex_token_count(last=2_000, window=258_400, primary=34.0, secondary=61.0)],
        at=LAUNCHED_AT + timedelta(hours=2),
        started=LAUNCHED_AT - timedelta(days=2),
    )

    reader = CodexUsageReader(
        sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT + timedelta(hours=3)
    )

    assert [window.used_percent for window in reader.limits().windows] == [34.0, 61.0]


def test_an_idle_host_still_reports_the_window_that_is_still_open(
    tmp_path: Path, workspace: Path
) -> None:
    """Answering nothing would be indistinguishable from a provider that publishes nothing.

    `limit_lines` drops an agent with no windows, so a Codex gone quiet for a few days would
    vanish from the block exactly as `cursor-agent` permanently does — and DEC-061 requires
    those two stay apart. The weekly window is still open and its figure is still on disk.
    """
    _account_rollout(
        tmp_path,
        workspace,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [_codex_token_count(last=1_000, window=258_400, primary=5.0, secondary=61.0)],
        at=LAUNCHED_AT,
        started=LAUNCHED_AT,
    )

    reader = CodexUsageReader(
        sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT + timedelta(days=4)
    )

    assert [window.label for window in reader.limits().windows] == ["week"]


def test_an_account_reading_says_when_it_was_observed(tmp_path: Path, workspace: Path) -> None:
    """Taken from the record's own timestamp: the provider's statement, not the filesystem's.

    An mtime agrees with it today and is still a filesystem attribute that a copy, a restore or
    a backup tool can move. The record is what Codex itself wrote down.
    """
    record = _codex_token_count(last=1_000, window=258_400, primary=5.0, secondary=61.0)
    record["timestamp"] = "2026-08-27T06:30:00.500Z"
    _account_rollout(
        tmp_path, workspace, "aaaaaaaa-0000-4000-8000-000000000000", [record], at=LAUNCHED_AT
    )

    limits = CodexUsageReader(
        sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT + timedelta(hours=2)
    ).limits()

    assert limits.observed_at == datetime(2026, 8, 27, 6, 30, 0, 500_000, tzinfo=UTC)


def test_a_rollout_with_no_accounting_record_answers_empty_for_the_account_too(
    tmp_path: Path, workspace: Path
) -> None:
    """A session that has opened its rollout but not yet taken a turn writes no `token_count`."""
    _account_rollout(
        tmp_path, workspace, "aaaaaaaa-0000-4000-8000-000000000000", [], at=LAUNCHED_AT
    )

    limits = CodexUsageReader(
        sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT
    ).limits()

    assert limits.windows == ()
    assert limits.observed_at is None


@pytest.mark.parametrize("poison", ["1e400", "-1e400"])
def test_an_infinite_number_in_a_provider_file_costs_no_screen(
    tmp_path: Path, workspace: Path, poison: str
) -> None:
    """`json.loads("1e400")` is `inf` — strictly valid JSON, and `int(inf)` is an OverflowError.

    `OverflowError` is an `ArithmeticError`, not a `ValueError`, so it escaped the catch set
    that makes these readers total. The account read calls *every* reader on every render of
    the limits block, so one poisoned file anywhere on the host took out the whole block rather
    than one session's line — which is why this is worth a guard at the conversion itself and
    not only a wider `except`.
    """
    raw = (
        '{"type":"event_msg","payload":{"type":"token_count",'
        '"info":{"last_token_usage":{"total_tokens":' + poison + "},"
        '"model_context_window":' + poison + "},"
        '"rate_limits":{"primary":{"used_percent":5.0,"window_minutes":' + poison + ","
        '"resets_at":1787341653}}}}'
    )
    path = (
        tmp_path
        / "codex-sessions"
        / f"{LAUNCHED_AT:%Y}"
        / f"{LAUNCHED_AT:%m}"
        / f"{LAUNCHED_AT:%d}"
        / "rollout-2026-08-27T06-05-00-aaaaaaaa-0000-4000-8000-000000000000.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"type":"session_meta","payload":{"cwd":"'
        + str(workspace.resolve())
        + '"}}\n'
        + raw
        + "\n",
        encoding="utf-8",
    )

    reader = CodexUsageReader(sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT)

    assert reader.limits().windows == ()
    assert reader.read(_query("codex", workspace)) is not None


def test_a_reader_missing_its_profile_label_costs_no_screen_either() -> None:
    """`ProfileUsageReaders` takes `Iterable[object]` with no protocol, so this is reachable.

    The label used to be read *outside* the try that makes this total, so a reader without one
    raised straight through the boundary the docstring promises never raises.
    """

    class _Unlabelled:
        profiles = frozenset({ProfileId("codex")})

        def read(self, query: UsageQuery) -> None:  # noqa: ARG002 - never reached here
            return None

        def limits(self) -> AgentLimits:
            return AgentLimits(ProfileId("codex"))

    assert ProfileUsageReaders((_Unlabelled(),)).limits() == ()


def test_the_provenance_fields_must_be_named() -> None:
    """Positional construction is how a field inserted mid-dataclass silently miscompiles.

    Measured during this stage: `observed_at` was added between `windows` and `stale_source`,
    and one of the two callers still passing three positional arguments put the borrowed-cache
    string into the timestamp field and left the provenance stamp `None` — so a figure this
    project cannot vouch for rendered as though it had been measured here. The payload stays
    positional because it is the answer; the two fields *about* the answer are keyword-only, so
    the next field added between them cannot shift anything.
    """
    with pytest.raises(TypeError):
        AgentLimits(ProfileId("claude"), (UsageWindow("5h", 2.0),), "status-line cache")  # type: ignore[misc]


def test_a_session_running_since_the_measured_worst_case_is_still_reached(
    tmp_path: Path, workspace: Path
) -> None:
    """Eight days: the largest start-to-last-write gap across 289 rollouts on the real host.

    Deliberately a literal rather than `_ACCOUNT_ROLLOUT_DAYS - 1`. The first version of this
    test derived its fixture from the constant, so shrinking the bound shrank the fixture with
    it and the test stayed green against the exact mutation it was written to catch — a test
    that measures the code against itself. The eight comes from outside the code, which is the
    only place a bound's justification can come from.

    Nine directories, one per day, with the newest write in the oldest: a session begun eight
    days ago and still running, which is what a long-lived managed agent session is.
    """
    _account_rollout(
        tmp_path,
        workspace,
        "aaaaaaaa-0000-4000-8000-000000000000",
        [_codex_token_count(last=1_000, window=258_400, primary=34.0, secondary=61.0)],
        at=LAUNCHED_AT + timedelta(hours=2),
        started=LAUNCHED_AT - timedelta(days=8),
    )
    for offset in range(8):
        _account_rollout(
            tmp_path,
            workspace,
            f"bbbbbbbb-0000-4000-8000-{offset:012d}",
            [_codex_token_count(last=2_000, window=258_400, primary=87.0, secondary=54.0)],
            at=LAUNCHED_AT + timedelta(hours=1),
            started=LAUNCHED_AT - timedelta(days=offset),
        )

    reader = CodexUsageReader(
        sessions_root=tmp_path / "codex-sessions", now=lambda: LAUNCHED_AT + timedelta(hours=3)
    )

    assert [window.used_percent for window in reader.limits().windows] == [34.0, 61.0]


def test_the_directory_bound_is_wide_enough_for_the_measured_worst_case() -> None:
    """Eight days was the largest start-to-last-write gap across 289 real rollouts.

    Stated as a check rather than a comment because the bound's justification is a measurement,
    and a measurement that lives only in prose stops being checked the first time someone tunes
    the constant down to make a slow test faster.
    """
    assert _ACCOUNT_ROLLOUT_DAYS >= 8
