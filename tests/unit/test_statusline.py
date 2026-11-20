"""The status-line hop: record a limits reading, then run the owner's own status line.

`remote-agents statusline --then <previous>` is what the installer wraps around the command
already in the owner's `statusLine` setting. Claude Code runs it on every status-line update
in every session, so it is held to the same rule as `agent-event`: reachable without the
composition root, stdlib-only at module scope, and never the reason the owner's bar breaks --
whatever stdin carried, the previous command runs on the same bytes and its exit status is
the hop's.

What it stores is a host fact (the plan's windows), not a session's words -- the DEC-013
nuance the module docstring states -- plus, for a managed session only, Claude's own context
figure under that session's id (DEC-089 as amended). These tests pin that it stores *only*
those, with exactly the keys named.
"""

from __future__ import annotations

import ast
import io
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from check_imports import COMPOSITION_ROOTS, find_violations

_REPOSITORY = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPOSITORY / "src"
_MODULE = _SOURCE_ROOT / "remote_agents" / "statusline.py"
_FIXTURE = (
    _REPOSITORY
    / "tests"
    / "provider_contract"
    / "fixtures"
    / "claude"
    / "statusline_stdin"
    / "documented-example.json"
)


def _documented_payload() -> bytes:
    return _FIXTURE.read_bytes()


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, payload: bytes | None) -> None:
    """Stand in for the process stdin; `None` is a process started with no stdin at all."""
    if payload is None:
        monkeypatch.setattr(sys, "stdin", None)
        return

    class _Stdin:
        buffer = io.BytesIO(payload)

    monkeypatch.setattr(sys, "stdin", _Stdin())


def _run_hop(
    *arguments: str, payload: bytes, state_directory: Path
) -> subprocess.CompletedProcess[bytes]:
    """Drive the shipped entry point as Claude Code would, always into a scratch directory.

    `state_directory` is required, not defaulted: the hop's own default is the owner's real
    state directory, and a test that forgot `--state-dir` once wrote the documented example
    into it -- a fake reading, under the file the live limits pane reads (2026-09-14).
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "remote_agents",
            "statusline",
            "--state-dir",
            str(state_directory),
            *arguments,
        ],
        input=payload,
        capture_output=True,
        check=False,
    )


# --- the write ---------------------------------------------------------------------------


def test_the_documented_payload_is_recorded_as_received_with_a_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remote_agents.statusline import run_statusline

    _feed_stdin(monkeypatch, _documented_payload())
    before = 1_700_000_000.0

    assert run_statusline(["--state-dir", str(tmp_path)]) == 0

    written = tmp_path / "claude-limits.json"
    document = json.loads(written.read_text(encoding="utf-8"))
    assert set(document) == {"rate_limits", "recorded_at"}
    # Written through as received: both windows, and the spend window the docs also show.
    assert document["rate_limits"] == json.loads(_documented_payload())["rate_limits"]
    windows = document["rate_limits"]
    assert windows["five_hour"] == {"used_percentage": 23.5, "resets_at": 1738425600}
    assert windows["seven_day"] == {"used_percentage": 41.2, "resets_at": 1738857600}
    assert document["recorded_at"] > before
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    # Atomic: the temp name was replaced onto the target, not left beside it.
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["claude-limits.json"]


def test_a_payload_without_rate_limits_leaves_the_previous_reading_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rate_limits` is absent for non-Pro/Max users and before the first API response."""
    from remote_agents.statusline import run_statusline

    previous = tmp_path / "claude-limits.json"
    previous.write_bytes(b'{"rate_limits": {"five_hour": {}}, "recorded_at": 1}')
    payload = json.loads(_documented_payload())
    del payload["rate_limits"]
    _feed_stdin(monkeypatch, json.dumps(payload).encode("utf-8"))

    assert run_statusline(["--state-dir", str(tmp_path)]) == 0

    assert previous.read_bytes() == b'{"rate_limits": {"five_hour": {}}, "recorded_at": 1}'
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["claude-limits.json"]


def test_malformed_json_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from remote_agents.statusline import run_statusline

    _feed_stdin(monkeypatch, b'{"rate_limits": ')

    assert run_statusline(["--state-dir", str(tmp_path)]) == 0

    assert list(tmp_path.iterdir()) == []


def test_empty_stdin_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from remote_agents.statusline import run_statusline

    _feed_stdin(monkeypatch, b"")

    assert run_statusline(["--state-dir", str(tmp_path)]) == 0

    assert list(tmp_path.iterdir()) == []


def test_a_missing_state_directory_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service owns that directory; the hop writes into it or not at all."""
    from remote_agents.statusline import run_statusline

    absent = tmp_path / "never-made"
    _feed_stdin(monkeypatch, _documented_payload())

    assert run_statusline(["--state-dir", str(absent)]) == 0

    assert not absent.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through directory modes")
def test_an_unwritable_state_directory_is_skipped_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    from remote_agents.statusline import run_statusline

    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    _feed_stdin(monkeypatch, _documented_payload())
    try:
        assert run_statusline(["--state-dir", str(locked)]) == 0
        assert list(locked.iterdir()) == []
    finally:
        locked.chmod(0o700)
    assert capfd.readouterr() == ("", "")


# --- the forward -------------------------------------------------------------------------


def test_the_previous_command_runs_on_the_same_bytes_and_sets_the_exit_status(
    tmp_path: Path,
) -> None:
    payload = _documented_payload()

    completed = _run_hop("--then", "cat; exit 3", payload=payload, state_directory=tmp_path)

    assert completed.returncode == 3
    assert completed.stdout == payload


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b'{"rate_limits": ', id="malformed"),
        pytest.param(b'{"hook_event_name": "Status"}', id="no-rate-limits"),
    ],
)
def test_every_failure_to_record_still_runs_the_previous_command(
    tmp_path: Path, payload: bytes
) -> None:
    """The hop must never be the reason the owner's status bar breaks."""
    absent = tmp_path / "never-made"

    completed = _run_hop(
        "--then", "printf rendered; exit 5", payload=payload, state_directory=absent
    )

    assert completed.returncode == 5
    assert completed.stdout == b"rendered"
    assert completed.stderr == b""
    assert not absent.exists()


def test_a_process_with_no_stdin_still_runs_the_previous_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    from remote_agents.statusline import run_statusline

    _feed_stdin(monkeypatch, None)

    code = run_statusline(["--state-dir", str(tmp_path), "--then", "printf rendered; exit 7"])

    assert code == 7
    assert capfd.readouterr().out == "rendered"
    assert list(tmp_path.iterdir()) == []


def test_without_a_previous_command_it_prints_nothing_and_exits_zero(tmp_path: Path) -> None:
    completed = _run_hop(payload=_documented_payload(), state_directory=tmp_path)

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""
    assert (tmp_path / "claude-limits.json").exists()


def test_bootstrap_delegates_to_the_one_implementation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main(["statusline"])` keeps working for a caller that already has `bootstrap` loaded."""
    from remote_agents import bootstrap

    seen: list[tuple[str | None, Path | None]] = []

    def _hop(then: str | None, state_directory: Path | None) -> int:
        seen.append((then, state_directory))
        return 9

    monkeypatch.setattr(bootstrap, "hop_from_stdin", _hop)

    assert bootstrap.main(["statusline", "--then", "cat", "--state-dir", "/tmp/x"]) == 9
    assert seen == [("cat", Path("/tmp/x"))]


# --- the cost ----------------------------------------------------------------------------


def test_the_hop_does_not_load_the_composition_root(tmp_path: Path) -> None:
    """Mirrors the `agent-event` probe: the module set, not a wall-clock number."""
    probe = (
        "import sys, runpy\n"
        f"sys.argv = ['remote_agents', 'statusline', '--state-dir', {str(tmp_path)!r}]\n"
        "try:\n"
        "    runpy.run_module('remote_agents', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "loaded = 'remote_agents.bootstrap' in sys.modules\n"
        "print('loaded' if loaded else 'lean', len(sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        input=_documented_payload(),
        capture_output=True,
        check=True,
    )

    verdict, modules = completed.stdout.decode("utf-8").split()
    assert verdict == "lean"
    assert int(modules) < 300


def test_the_module_imports_only_the_standard_library_at_module_scope() -> None:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"), filename=str(_MODULE))

    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None and node.level == 0
            imported.add(node.module.partition(".")[0])

    assert imported, "the module has no imports at all, which is not what this test expects"
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names


def test_the_hop_is_an_enumerated_composition_root() -> None:
    """DEC-015: the set is widened by name, never by position."""
    assert COMPOSITION_ROOTS == frozenset({"bootstrap.py", "agent_event.py", "statusline.py"})
    assert find_violations(_SOURCE_ROOT) == []


# --- Stage 2 gate, round 2: signal deaths, abandoned temporaries, and the unexpected ------


def test_a_previous_command_killed_by_a_signal_exits_with_the_shell_convention(
    tmp_path: Path,
) -> None:
    """`sh` reports a signal death as 128+N; the hop must not hand back the raw negative."""
    completed = _run_hop(
        "--then", "kill -TERM $$", payload=_documented_payload(), state_directory=tmp_path
    )

    assert completed.returncode == 143


def test_a_stale_temporary_from_a_killed_hop_is_collected_and_a_fresh_one_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code cancels an in-flight hop on a newer update; what it leaves is swept."""
    from remote_agents.statusline import run_statusline

    stale = tmp_path / ".claude-limits.json.deadbeef0000.tmp"
    fresh = tmp_path / ".claude-limits.json.cafef00d0000.tmp"
    stale.write_bytes(b"{")
    fresh.write_bytes(b"{")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    _feed_stdin(monkeypatch, _documented_payload())

    assert run_statusline(["--state-dir", str(tmp_path)]) == 0

    assert not stale.exists()
    assert fresh.exists()
    assert (tmp_path / "claude-limits.json").is_file()


def test_a_leftover_temporary_named_after_this_pid_cannot_block_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp name once carried the pid; a leftover plus pid reuse then skipped every write."""
    from remote_agents.statusline import run_statusline

    (tmp_path / f".claude-limits.json.{os.getpid()}.tmp").write_bytes(b"{")
    _feed_stdin(monkeypatch, _documented_payload())

    assert run_statusline(["--state-dir", str(tmp_path)]) == 0

    assert (tmp_path / "claude-limits.json").is_file()


def test_an_unexpected_error_while_recording_still_runs_the_previous_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The contract is every failure, not every failure this module foresaw."""
    import remote_agents.statusline as hop
    from remote_agents.statusline import run_statusline

    def _explode(payload: bytes, state_directory: Path | None) -> None:
        raise RecursionError("pathological nesting")

    monkeypatch.setattr(hop, "record_limits", _explode)
    _feed_stdin(monkeypatch, _documented_payload())

    code = run_statusline(["--state-dir", str(tmp_path), "--then", "printf rendered; exit 4"])

    assert code == 4
    assert capfd.readouterr().out == "rendered"


# --- Sub-plan 5 Task 1.1: one managed session's context ----------------------------------
#
# The hop also keeps the provider's own context figure for a managed session, because that
# figure exists nowhere else on the host (BL-118; DEC-089 amended). It keeps exactly three
# keys, under the session's id, and only when the id and the figure are both well formed.

_SESSION = "0eaa3b3e-d567-4349-9809-d1b04e1ce23b"


def _context_payload(**context_window: object) -> bytes:
    """A payload shaped like the one captured on 2026-11-20 from Claude Code 2.1.329."""
    payload = json.loads(_documented_payload())
    payload.update(
        {
            "cwd": "/home/user/dev/somewhere",
            "transcript_path": "/home/user/.claude/projects/x/abc123.jsonl",
            "workspace": {"current_dir": "/home/user/dev/somewhere"},
            "cost": {"total_cost_usd": 1.25},
            "context_window": {
                "total_input_tokens": 415000,
                "total_output_tokens": 9000,
                "current_usage": {"input_tokens": 12, "cache_read_input_tokens": 414988},
                "remaining_percentage": 58.5,
                **context_window,
            },
        }
    )
    return json.dumps(payload).encode("utf-8")


def _context_file(state_directory: Path, session: str = _SESSION) -> Path:
    return state_directory / "claude-context" / f"{session}.json"


def test_a_managed_session_s_context_is_recorded_with_exactly_three_keys(
    tmp_path: Path,
) -> None:
    from remote_agents.statusline import record_context

    before = time.time()
    record_context(
        _context_payload(used_percentage=41.5, context_window_size=1_000_000),
        tmp_path,
        {"REMOTE_AGENTS_SESSION_ID": _SESSION},
    )

    written = _context_file(tmp_path)
    document = json.loads(written.read_text(encoding="utf-8"))
    assert set(document) == {"used_percentage", "context_window_size", "recorded_at"}
    assert document["used_percentage"] == 41.5
    assert document["context_window_size"] == 1_000_000
    assert before <= document["recorded_at"] <= time.time()
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.parent.stat().st_mode) == 0o700
    # Atomic, and nothing else of the payload lands anywhere in the directory.
    assert sorted(entry.name for entry in written.parent.iterdir()) == [f"{_SESSION}.json"]
    text = written.read_text(encoding="utf-8")
    for leaked in ("transcript_path", "cwd", "/home/user", "abc123", "Opus", "cost"):
        assert leaked not in text


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"REMOTE_AGENTS_SESSION_ID": ""},
        {"REMOTE_AGENTS_SESSION_ID": "-rf"},
        {"REMOTE_AGENTS_SESSION_ID": "a" * 200},
        {"REMOTE_AGENTS_SESSION_ID": "../../escape"},
        {"REMOTE_AGENTS_SESSION_ID": "two\nlines"},
    ],
    ids=["absent", "empty", "dash", "200-chars", "traversal", "newline"],
)
def test_no_context_is_recorded_without_a_well_formed_session_id(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    from remote_agents.statusline import record_context

    record_context(_context_payload(used_percentage=41.5), tmp_path, environment)

    assert not (tmp_path / "claude-context").exists()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "figure",
    [None, "41.5", True, float("nan"), -1, 101, [41.5], {"value": 41.5}],
    ids=["null", "string", "bool", "nan", "negative", "over-100", "list", "object"],
)
def test_no_context_is_recorded_without_a_numeric_percentage(
    tmp_path: Path, figure: object
) -> None:
    """Null is the live case: the captured payload carries it before the first turn."""
    from remote_agents.statusline import record_context

    payload = _context_payload(used_percentage=figure, context_window_size=1_000_000)
    record_context(payload, tmp_path, {"REMOTE_AGENTS_SESSION_ID": _SESSION})

    assert not _context_file(tmp_path).exists()


def test_no_context_is_recorded_when_the_payload_has_no_context_window(tmp_path: Path) -> None:
    from remote_agents.statusline import record_context

    payload = json.loads(_documented_payload())
    del payload["context_window"]
    for document in (payload, [1, 2], "text"):
        record_context(
            json.dumps(document).encode("utf-8"), tmp_path, {"REMOTE_AGENTS_SESSION_ID": _SESSION}
        )
    record_context(b"{not json", tmp_path, {"REMOTE_AGENTS_SESSION_ID": _SESSION})

    assert not _context_file(tmp_path).exists()


def test_a_window_size_that_is_not_a_positive_integer_is_recorded_as_null(
    tmp_path: Path,
) -> None:
    from remote_agents.statusline import record_context

    for size in ("1000000", -5, 0, True, 1.5, None):
        record_context(
            _context_payload(used_percentage=12, context_window_size=size),
            tmp_path,
            {"REMOTE_AGENTS_SESSION_ID": _SESSION},
        )
        document = json.loads(_context_file(tmp_path).read_text(encoding="utf-8"))
        assert document["used_percentage"] == 12
        assert document["context_window_size"] is None, size


def test_a_missing_state_directory_gets_no_context_directory(tmp_path: Path) -> None:
    from remote_agents.statusline import record_context

    missing = tmp_path / "not-there"
    record_context(
        _context_payload(used_percentage=41.5), missing, {"REMOTE_AGENTS_SESSION_ID": _SESSION}
    )

    assert not missing.exists()


def test_a_context_directory_that_is_a_link_is_not_written_through(tmp_path: Path) -> None:
    from remote_agents.statusline import record_context

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "claude-context").symlink_to(elsewhere)

    record_context(
        _context_payload(used_percentage=41.5), state, {"REMOTE_AGENTS_SESSION_ID": _SESSION}
    )

    assert list(elsewhere.iterdir()) == []


def test_the_hop_records_both_files_from_one_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remote_agents.statusline import run_statusline

    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", _SESSION)
    _feed_stdin(monkeypatch, _context_payload(used_percentage=41.5, context_window_size=200_000))

    assert run_statusline(["--state-dir", str(tmp_path)]) == 0

    limits = json.loads((tmp_path / "claude-limits.json").read_text(encoding="utf-8"))
    assert set(limits) == {"rate_limits", "recorded_at"}
    assert limits["rate_limits"] == json.loads(_documented_payload())["rate_limits"]
    context = json.loads(_context_file(tmp_path).read_text(encoding="utf-8"))
    assert context["used_percentage"] == 41.5


def test_an_unmanaged_session_still_records_only_the_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remote_agents.statusline import run_statusline

    monkeypatch.delenv("REMOTE_AGENTS_SESSION_ID", raising=False)
    _feed_stdin(monkeypatch, _context_payload(used_percentage=41.5))

    assert run_statusline(["--state-dir", str(tmp_path)]) == 0

    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["claude-limits.json"]


@pytest.mark.parametrize("blocker", ["file", "unwritable"])
def test_a_failure_to_record_the_context_never_changes_the_bar(
    tmp_path: Path, blocker: str
) -> None:
    """Exit status and output are the owner's command's, and the limits file still lands."""
    target = tmp_path / "claude-context"
    if blocker == "file":
        target.write_bytes(b"not a directory")
    else:
        if os.geteuid() == 0:
            pytest.skip("root writes into a 0500 directory, so it cannot stand for unwritable")
        target.mkdir(mode=0o500)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "remote_agents",
                "statusline",
                "--state-dir",
                str(tmp_path),
                "--then",
                "printf rendered; exit 3",
            ],
            input=_context_payload(used_percentage=41.5),
            capture_output=True,
            check=False,
            env={**os.environ, "REMOTE_AGENTS_SESSION_ID": _SESSION},
        )
    finally:
        if target.is_dir():
            target.chmod(0o700)

    assert completed.returncode == 3
    assert completed.stdout == b"rendered"
    assert completed.stderr == b""
    assert (tmp_path / "claude-limits.json").is_file()
    assert not _context_file(tmp_path).exists()


def test_an_unexpected_error_while_recording_the_context_still_runs_the_previous_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    import remote_agents.statusline as hop
    from remote_agents.statusline import run_statusline

    def _explode(payload: bytes, state_directory: Path | None, environment: object) -> None:
        raise RecursionError("pathological nesting")

    monkeypatch.setattr(hop, "record_context", _explode)
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", _SESSION)
    _feed_stdin(monkeypatch, _context_payload(used_percentage=41.5))

    code = run_statusline(["--state-dir", str(tmp_path), "--then", "printf rendered; exit 4"])

    assert code == 4
    assert capfd.readouterr().out == "rendered"
    assert (tmp_path / "claude-limits.json").is_file()


def test_a_stale_context_temporary_is_collected(tmp_path: Path) -> None:
    from remote_agents.statusline import record_context

    directory = tmp_path / "claude-context"
    directory.mkdir(mode=0o700)
    stale = directory / f".{_SESSION}.json.deadbeef0000.tmp"
    stale.write_bytes(b"{")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))

    record_context(
        _context_payload(used_percentage=41.5), tmp_path, {"REMOTE_AGENTS_SESSION_ID": _SESSION}
    )

    assert not stale.exists()
    assert _context_file(tmp_path).is_file()
