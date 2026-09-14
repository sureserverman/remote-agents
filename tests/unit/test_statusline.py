"""The status-line hop: record a limits reading, then run the owner's own status line.

`remote-agents statusline --then <previous>` is what the installer wraps around the command
already in the owner's `statusLine` setting. Claude Code runs it on every status-line update
in every session, so it is held to the same rule as `agent-event`: reachable without the
composition root, stdlib-only at module scope, and never the reason the owner's bar breaks --
whatever stdin carried, the previous command runs on the same bytes and its exit status is
the hop's.

What it stores is a host fact (the plan's windows), not a session's words -- the DEC-013
nuance the module docstring states; these tests pin that it stores *only* that.
"""

from __future__ import annotations

import ast
import io
import json
import os
import stat
import subprocess
import sys
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
