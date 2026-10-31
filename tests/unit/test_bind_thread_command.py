"""`remote-agents bind-thread`, then `session-id`: a fresh thread gets a session only by binding.

These drive the real entry point with the environment a `!` shell has. Exit 3 means refused, and
every refusal leaves the earlier state standing, because a handoff stamped wrongly is worse than
none (DEC-128).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.__main__ import main
from remote_agents.adapters.agents.hook_identity import RosterEntry, roster_path, write_roster
from remote_agents.adapters.agents.thread_binding import BINDING_DIRECTORY, mint_nonce
from remote_agents.adapters.agents.turn_markers import FileConversationMarks, FileTurnMarkers
from remote_agents.domain.models import SessionId

_MINTED = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)
_THREAD = f"{int(_MINTED.timestamp() * 1000):012x}"
_THREAD = f"{_THREAD[:8]}-{_THREAD[8:12]}-7abc-8def-0123456789ab"
_SECOND_THREAD = _THREAD[:-1] + "c"


@pytest.fixture
def spool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv("REMOTE_AGENTS_SESSION_ID", raising=False)
    monkeypatch.delenv("PLANNING_HANDOFF_ROOT", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    return directory


def _roster(spool: Path, *sessions: tuple[str, str | None]) -> None:
    write_roster(
        roster_path(spool, "codex"),
        [
            RosterEntry(session, "/somewhere", _MINTED - timedelta(minutes=1), resumed)
            for session, resumed in sessions
        ],
    )


def _bind(spool: Path, session: str, nonce: str | None) -> int:
    flags = ["--activity-dir", str(spool), "--session", session]
    return main(["bind-thread", *flags, *([] if nonce is None else ["--nonce", nonce])])


def _named(spool: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    code = main(["session-id", "--activity-dir", str(spool)])
    return code, capsys.readouterr().out


def _bound_session(spool: Path) -> str:
    session = str(SessionId.new())
    _roster(spool, (session, None))
    assert _bind(spool, session, mint_nonce(spool, session)) == 0
    return session


def test_fresh_thread_bound_then_named(
    spool: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = str(SessionId.new())
    _roster(spool, (session, None))
    assert _named(spool, capsys) == (3, "")
    nonce = mint_nonce(spool, session)
    capsys.readouterr()
    assert _bind(spool, session, nonce) == 0
    assert capsys.readouterr() == ("", "")
    assert _named(spool, capsys) == (0, f"{session}\n")
    monkeypatch.chdir(tmp_path)
    assert _named(spool, capsys) == (0, f"{session}\n")


def test_a_wrong_nonce_exits_3_says_why_and_writes_nothing(
    spool: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = str(SessionId.new())
    _roster(spool, (session, None))
    mint_nonce(spool, session)
    guess = "f" * 64
    assert _bind(spool, session, guess) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the nonce does not match" in captured.err
    assert guess not in captured.err
    assert _named(spool, capsys) == (3, "")


@pytest.mark.parametrize("why", ["no flag", "nothing waiting"])
def test_a_missing_nonce_exits_3(spool: Path, capsys: pytest.CaptureFixture[str], why: str) -> None:
    session = str(SessionId.new())
    _roster(spool, (session, None))
    if why == "no flag":
        mint_nonce(spool, session)
        assert _bind(spool, session, None) == 3
    else:
        assert _bind(spool, session, "f" * 64) == 3
    assert capsys.readouterr().err
    assert _named(spool, capsys) == (3, "")


def test_a_second_thread_replaying_a_used_nonce_leaves_the_first_binding(
    spool: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = str(SessionId.new())
    _roster(spool, (session, None))
    nonce = mint_nonce(spool, session)
    assert _bind(spool, session, nonce) == 0
    monkeypatch.setenv("CODEX_THREAD_ID", _SECOND_THREAD)
    assert _bind(spool, session, nonce) == 3
    assert _named(spool, capsys) == (3, "")
    monkeypatch.setenv("CODEX_THREAD_ID", _THREAD)
    assert _named(spool, capsys) == (0, f"{session}\n")


def test_a_shell_with_no_thread_cannot_bind(
    spool: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID")
    session = str(SessionId.new())
    assert _bind(spool, session, mint_nonce(spool, session)) == 3
    assert "no thread" in capsys.readouterr().err


def test_an_unmanaged_thread_stays_refused_while_another_is_bound(
    spool: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_session(spool)
    monkeypatch.setenv("CODEX_THREAD_ID", _SECOND_THREAD)
    assert _named(spool, capsys) == (3, "")


def test_a_thread_bound_to_a_session_off_the_roster_exits_3(
    spool: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _bound_session(spool)
    _roster(spool, (str(SessionId.new()), None))
    assert _named(spool, capsys) == (3, "")


def test_a_later_session_can_resume_a_thread_whose_binder_ended(
    spool: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _bound_session(spool)
    resumer = str(SessionId.new())
    _roster(spool, (resumer, _THREAD))
    assert _named(spool, capsys) == (0, f"{resumer}\n")


def test_a_bound_session_that_resumed_its_own_thread_is_still_named(
    spool: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _bound_session(spool)
    _roster(spool, (session, _THREAD))
    assert _named(spool, capsys) == (0, f"{session}\n")


def test_two_sessions_claiming_one_thread_exit_3(
    spool: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _bound_session(spool)
    _roster(spool, (session, None), (str(SessionId.new()), _THREAD))
    assert _named(spool, capsys) == (3, "")


@pytest.mark.parametrize("mark", ["conversation", "turn"])
def test_a_mark_that_disagrees_exits_3(
    spool: Path, capsys: pytest.CaptureFixture[str], mark: str
) -> None:
    session = _bound_session(spool)
    if mark == "conversation":
        FileConversationMarks(spool).record(session, _SECOND_THREAD)
    else:
        FileTurnMarkers(spool).start(session, _SECOND_THREAD)
    assert _named(spool, capsys) == (3, "")


def test_a_mark_that_agrees_keeps_the_binding(
    spool: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _bound_session(spool)
    FileConversationMarks(spool).record(session, _THREAD)
    FileTurnMarkers(spool).start(session, _THREAD)
    assert _named(spool, capsys) == (0, f"{session}\n")


def test_a_binding_directory_others_can_open_fails_closed(
    spool: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _bound_session(spool)
    (spool / BINDING_DIRECTORY).chmod(0o750)
    assert _named(spool, capsys) == (3, "")
    assert _bind(spool, session, "f" * 64) == 3


def test_a_linked_binding_directory_fails_closed(
    spool: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    session = str(SessionId.new())
    resumer = str(SessionId.new())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (spool / BINDING_DIRECTORY).symlink_to(elsewhere)
    _roster(spool, (session, None), (resumer, _THREAD))
    assert _named(spool, capsys) == (3, "")
    assert _bind(spool, session, "f" * 64) == 3
    assert list(elsewhere.iterdir()) == []


def test_an_unknown_flag_is_a_usage_error_that_echoes_nothing(
    spool: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["bind-thread", "--activity-dir", str(spool), "--nonse=hunter2"])
    assert stopped.value.code == 2
    assert "hunter2" not in capsys.readouterr().err
