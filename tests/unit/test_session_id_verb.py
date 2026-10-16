"""`remote-agents session-id`: the calling shell's managed session, by its provider's own rule.

A handoff writer runs as an agent's tool command and stamps the session it belongs to. Under
Codex that shell is spawned by the shared app-server daemon and carries the environment of
whichever pane started the daemon (measured 2026-10-16, `tests/live/test_codex_tool_shell_
identity.py`), so its session variable can name another session. These cases drive the real
entry point, `remote_agents.__main__:main`, with the environment and folder a shell would have.

Exit codes are the contract Sub-plan 3's writer depends on: 0 with the id alone on one line,
3 for a shell no managed session owns (the writer then does nothing), 2 for a usage error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.__main__ import main
from remote_agents.adapters.agents.hook_identity import RosterEntry, roster_path, write_roster
from remote_agents.domain.models import SessionId

# A version-7 thread id minted at 2026-10-16T07:00:00Z, as Codex mints them.
_MINTED = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)
_THREAD = f"{int(_MINTED.timestamp() * 1000):012x}"
_THREAD = f"{_THREAD[:8]}-{_THREAD[8:12]}-7abc-8def-0123456789ab"


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A tool shell with neither variable set, working in its own folder; returns the spool."""
    monkeypatch.delenv("REMOTE_AGENTS_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    return spool


def _roster(spool: Path, *entries: RosterEntry) -> None:
    write_roster(roster_path(spool, "codex"), entries)


def _run(spool: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(["session-id", "--activity-dir", str(spool)])
    return code, capsys.readouterr().out


def test_session_id_command_prints_the_environment_id(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = str(SessionId.new())
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", session)
    assert _run(shell, capsys) == (0, f"{session}\n")


def test_session_id_command_resolves_a_codex_thread(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = str(SessionId.new())
    _roster(shell, RosterEntry(mine, str(Path.cwd().resolve()), _MINTED - timedelta(minutes=1)))
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (0, f"{mine}\n")


def test_session_id_command_ignores_a_stale_environment_id_under_a_codex_thread(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The daemon's starter's id, as B's shell carried A's: the roster says who, not the variable.
    starter, mine = str(SessionId.new()), str(SessionId.new())
    _roster(shell, RosterEntry(mine, str(Path.cwd().resolve()), _MINTED - timedelta(minutes=1)))
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", starter)
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (0, f"{mine}\n")


def test_session_id_command_never_falls_back_to_the_environment_for_an_unplaced_thread(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = str(SessionId.new())
    _roster(shell, RosterEntry(elsewhere, "/nowhere/else", _MINTED - timedelta(minutes=1)))
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", elsewhere)
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (3, "")


def test_session_id_command_exits_3_for_an_unmanaged_shell(
    shell: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(shell, capsys) == (3, "")


def test_session_id_command_exits_3_with_no_roster(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No roster means the service never published one; the inherited variable is still not
    # evidence under a shared daemon.
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", str(SessionId.new()))
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (3, "")


def test_session_id_command_refuses_a_malformed_environment_id(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", "../not-an-id")
    assert _run(shell, capsys) == (3, "")


def test_session_id_command_exits_2_on_a_usage_error(
    shell: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["session-id", "--no-such-flag"])
    assert raised.value.code == 2


# --- Tier-1 review of Task 1.2: where a wrong id was possible, and the rule's branches ---


@pytest.mark.parametrize("thread", [None, ""])
def test_session_id_command_never_trusts_a_rostered_id_without_its_thread(
    shell: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    thread: str | None,
) -> None:
    # A Codex shell whose thread variable is missing or empty still carries the daemon starter's
    # id. That id is a session on the Codex roster, so it is never the answer on its own.
    starter = str(SessionId.new())
    _roster(shell, RosterEntry(starter, str(Path.cwd().resolve()), _MINTED))
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", starter)
    if thread is not None:
        monkeypatch.setenv("CODEX_THREAD_ID", thread)
    assert _run(shell, capsys) == (3, "")


def test_session_id_command_names_an_unrostered_environment_session_beside_a_codex_roster(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A Claude shell on a host where Codex sessions are live: its session is on no Codex roster.
    claude, codex = str(SessionId.new()), str(SessionId.new())
    _roster(shell, RosterEntry(codex, str(Path.cwd().resolve()), _MINTED))
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", claude)
    assert _run(shell, capsys) == (0, f"{claude}\n")


def test_session_id_command_places_a_resumed_thread_in_any_folder(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resumed = str(SessionId.new())
    _roster(shell, RosterEntry(resumed, "/somewhere/else", _MINTED, resume_source_id=_THREAD))
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (0, f"{resumed}\n")


def test_session_id_command_gives_a_shared_folder_thread_to_the_newest_older_session(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    here = str(Path.cwd().resolve())
    older, newer, later = (str(SessionId.new()) for _ in range(3))
    _roster(
        shell,
        RosterEntry(older, here, _MINTED - timedelta(hours=1)),
        RosterEntry(newer, here, _MINTED - timedelta(minutes=1)),
        RosterEntry(later, here, _MINTED + timedelta(minutes=1)),
    )
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (0, f"{newer}\n")


def test_session_id_command_gives_an_untimed_thread_to_the_folder_s_only_session(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    only = str(SessionId.new())
    _roster(shell, RosterEntry(only, str(Path.cwd().resolve()), _MINTED))
    monkeypatch.setenv("CODEX_THREAD_ID", "not-a-version-7-id")
    assert _run(shell, capsys) == (0, f"{only}\n")


@pytest.mark.parametrize("spoil", ["shared", "symlink", "garbage", "bad-entry"])
def test_session_id_command_exits_3_on_an_untrusted_roster(
    shell: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    spoil: str,
) -> None:
    mine = str(SessionId.new())
    _roster(shell, RosterEntry(mine, str(Path.cwd().resolve()), _MINTED - timedelta(minutes=1)))
    path = roster_path(shell, "codex")
    if spoil == "shared":
        path.chmod(0o644)
    elif spoil == "symlink":
        real = tmp_path / "elsewhere.json"
        path.rename(real)
        path.symlink_to(real)
    elif spoil == "garbage":
        path.write_text("{not json", encoding="utf-8")
    else:
        path.write_text('{"sessions": [{"session_id": "x"}]}', encoding="utf-8")
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", mine)
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (3, "")


def test_session_id_command_places_a_resumed_thread_from_a_removed_folder(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resumed = str(SessionId.new())
    _roster(shell, RosterEntry(resumed, "/somewhere/else", _MINTED, resume_source_id=_THREAD))
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    gone = Path.cwd()
    gone.rmdir()
    assert _run(shell, capsys) == (0, f"{resumed}\n")


def test_session_id_command_answers_the_same_through_bootstrap(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from remote_agents import bootstrap

    session = str(SessionId.new())
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", session)
    assert bootstrap.main(["session-id", "--activity-dir", str(shell)]) == 0
    assert capsys.readouterr().out == f"{session}\n"


def test_session_id_command_defaults_to_the_production_spool(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from remote_agents.production import ProductionPaths

    home = shell.parent / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    spool = ProductionPaths.for_home(home).activity_directory
    spool.mkdir(parents=True, mode=0o700)
    mine = str(SessionId.new())
    _roster(spool, RosterEntry(mine, str(Path.cwd().resolve()), _MINTED - timedelta(minutes=1)))
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert main(["session-id"]) == 0
    assert capsys.readouterr().out == f"{mine}\n"


def test_session_id_command_exits_2_on_an_extra_argument(shell: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["session-id", "surplus"])
    assert raised.value.code == 2


# --- Stage 1 gate, round 1: a roster that cannot be trusted names nobody, for every shell ---


@pytest.mark.parametrize("thread", [None, ""])
def test_session_id_command_names_nobody_beside_an_untrusted_roster(
    shell: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    thread: str | None,
) -> None:
    # Without a trusted roster a daemon shell missing its thread variable cannot be told from a
    # Claude shell, and the variable may be the starter's: nobody's, as the hook says for `()`.
    _roster(shell, RosterEntry(str(SessionId.new()), str(Path.cwd().resolve()), _MINTED))
    roster_path(shell, "codex").chmod(0o644)
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", str(SessionId.new()))
    if thread is not None:
        monkeypatch.setenv("CODEX_THREAD_ID", thread)
    assert _run(shell, capsys) == (3, "")


def test_session_id_command_names_the_environment_session_when_no_roster_was_written(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The recorded limit, pinned so a change to it is deliberate: with no roster at all (no
    # service yet) a shell with no thread variable is named by its variable, as its hooks are.
    session = str(SessionId.new())
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", session)
    assert _run(shell, capsys) == (0, f"{session}\n")


def test_session_id_command_gives_a_hand_started_thread_to_the_folder_s_managed_session(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # hook_identity's recorded limit, pinned: a Codex started by hand in the folder a managed
    # session works in mints a newer thread, and the time rule places it under that session.
    managed = str(SessionId.new())
    _roster(shell, RosterEntry(managed, str(Path.cwd().resolve()), _MINTED - timedelta(hours=2)))
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (0, f"{managed}\n")


def test_session_id_command_exits_3_on_a_roster_too_deep_to_parse(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = roster_path(shell, "codex")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text("[" * 200_000, encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _run(shell, capsys) == (3, "")


def test_session_id_command_exits_3_on_any_unexpected_error(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from remote_agents import agent_event

    def broken(*_: object, **__: object) -> str:
        raise LookupError("a vertical that fails to load")

    monkeypatch.setattr(agent_event, "resolve_session_id", broken)
    assert _run(shell, capsys) == (3, "")


# --- Sub-plan 2 close-out evaluator: an empty roster the service wrote is trusted, not spoiled ---


def test_session_id_command_names_the_environment_session_beside_an_empty_codex_roster(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The service writes the roster even when no Codex session is live (DEC-126). A Claude shell
    # beside it is named by its variable; an empty roster is not an untrusted one.
    _roster(shell)
    session = str(SessionId.new())
    monkeypatch.setenv("REMOTE_AGENTS_SESSION_ID", session)
    assert _run(shell, capsys) == (0, f"{session}\n")


def test_session_id_command_writes_nothing_to_stdout_with_stderr_closed(
    shell: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A process started with fd 2 closed has `sys.stderr` None, and `print(file=None)` writes
    # to stdout: the contract says exit 3 prints nothing there.
    monkeypatch.setattr("sys.stderr", None)
    assert _run(shell, capsys) == (3, "")
