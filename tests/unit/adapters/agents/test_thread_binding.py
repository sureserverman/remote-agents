"""The binding store: one launch's nonce, spent once, ties one thread to one session.

The files decide who may stamp a handoff, so each case below is a way the files could lie.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from remote_agents.adapters.agents.thread_binding import (
    BINDING_DIRECTORY,
    UntrustedBinding,
    bound_session,
    consume_nonce,
    mint_nonce,
    prune,
)

_SESSION = "a" * 32
_OTHER = "b" * 32
_THREAD = "01a193e7-e8c8-7272-9c5f-6ee9e7749464"
_SECOND_THREAD = "01a193e8-0e07-7bc0-a39e-1b6af05448f0"


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    return directory


def _files(spool: Path) -> list[Path]:
    return [path for path in (spool / BINDING_DIRECTORY).rglob("*") if path.is_file()]


def test_pending_file_holds_only_a_hash_of_the_nonce(spool: Path) -> None:
    nonce = mint_nonce(spool, _SESSION)
    (pending,) = _files(spool)
    assert nonce not in pending.read_text(encoding="utf-8")
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600


def test_minting_makes_both_directories_so_no_shell_has_to(spool: Path) -> None:
    mint_nonce(spool, _SESSION)
    for name in ("pending", "threads"):
        assert stat.S_IMODE((spool / BINDING_DIRECTORY / name).stat().st_mode) == 0o700


def test_a_nonce_is_safe_to_type_after_a_flag(spool: Path) -> None:
    for _ in range(200):
        nonce = mint_nonce(spool, _SESSION)
        assert nonce.isalnum(), nonce


def test_consume_binds_the_thread_and_spends_the_nonce(spool: Path) -> None:
    nonce = mint_nonce(spool, _SESSION)
    assert consume_nonce(spool, _SESSION, nonce, _THREAD) is None
    assert bound_session(spool, _THREAD) == _SESSION
    (thread_file,) = _files(spool)
    assert thread_file.read_text(encoding="utf-8") == _SESSION
    assert stat.S_IMODE(thread_file.stat().st_mode) == 0o600


def test_a_wrong_nonce_writes_nothing_and_leaves_the_real_one_usable(spool: Path) -> None:
    nonce = mint_nonce(spool, _SESSION)
    assert consume_nonce(spool, _SESSION, "0" * 64, _THREAD) is not None
    assert bound_session(spool, _THREAD) is None
    assert consume_nonce(spool, _SESSION, nonce, _THREAD) is None


def test_a_session_with_no_nonce_waiting_is_refused(spool: Path) -> None:
    assert consume_nonce(spool, _SESSION, "0" * 64, _THREAD) is not None
    assert bound_session(spool, _THREAD) is None


def test_a_replay_from_a_second_thread_leaves_the_first_binding(spool: Path) -> None:
    nonce = mint_nonce(spool, _SESSION)
    assert consume_nonce(spool, _SESSION, nonce, _THREAD) is None
    assert consume_nonce(spool, _SESSION, nonce, _SECOND_THREAD) is not None
    assert bound_session(spool, _SECOND_THREAD) is None
    assert bound_session(spool, _THREAD) == _SESSION


def test_a_new_nonce_replaces_the_old_one(spool: Path) -> None:
    old = mint_nonce(spool, _SESSION)
    new = mint_nonce(spool, _SESSION)
    assert consume_nonce(spool, _SESSION, old, _THREAD) is not None
    assert consume_nonce(spool, _SESSION, new, _THREAD) is None


def test_a_bound_thread_cannot_be_rebound_by_another_session(spool: Path) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    assert consume_nonce(spool, _OTHER, mint_nonce(spool, _OTHER), _THREAD) is not None
    assert bound_session(spool, _THREAD) == _SESSION


def test_a_bind_refused_for_a_taken_thread_keeps_the_nonce(spool: Path) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    nonce = mint_nonce(spool, _OTHER)
    assert consume_nonce(spool, _OTHER, nonce, _THREAD) == "this thread is already bound"
    assert consume_nonce(spool, _OTHER, nonce, _SECOND_THREAD) is None
    assert bound_session(spool, _THREAD) == _SESSION


def test_a_dangling_link_at_the_thread_is_untrusted_and_keeps_the_nonce(
    spool: Path, tmp_path: Path
) -> None:
    nonce = mint_nonce(spool, _SESSION)
    threads = spool / BINDING_DIRECTORY / "threads"
    (threads / _THREAD).symlink_to(tmp_path / "nowhere")
    assert consume_nonce(spool, _SESSION, nonce, _THREAD) == (
        "the binding directory cannot be trusted"
    )
    assert consume_nonce(spool, _SESSION, nonce, _SECOND_THREAD) is None


def test_only_one_of_two_racing_consumers_wins(
    spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second arrives after the hash compared equal and before the claim."""
    nonce = mint_nonce(spool, _SESSION)
    real_rename = os.rename
    raced = []

    def rename_after_the_rival(source: str, target: str) -> None:
        if not raced:
            raced.append(True)
            assert consume_nonce(spool, _SESSION, nonce, _SECOND_THREAD) is None
        real_rename(source, target)

    monkeypatch.setattr(os, "rename", rename_after_the_rival)
    assert consume_nonce(spool, _SESSION, nonce, _THREAD) is not None
    assert bound_session(spool, _SECOND_THREAD) == _SESSION
    assert bound_session(spool, _THREAD) is None


def test_a_nonce_replaced_before_the_claim_is_given_back(
    spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old nonce compared equal, then a relaunch minted a new one, then the claim ran."""
    old = mint_nonce(spool, _SESSION)
    real_rename = os.rename
    fresh = []

    def rename_after_a_mint(source: str, target: str) -> None:
        if not fresh:
            fresh.append(mint_nonce(spool, _SESSION))
        real_rename(source, target)

    monkeypatch.setattr(os, "rename", rename_after_a_mint)
    assert consume_nonce(spool, _SESSION, old, _THREAD) is not None
    assert bound_session(spool, _THREAD) is None
    assert consume_nonce(spool, _SESSION, fresh[0], _THREAD) is None


def test_a_new_launchs_nonce_ends_the_sessions_earlier_binding_before_it_binds(
    spool: Path,
) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    mint_nonce(spool, _SESSION)
    assert bound_session(spool, _THREAD) is None


def test_a_binding_directory_that_cannot_be_read_is_untrusted(spool: Path) -> None:
    (spool / BINDING_DIRECTORY).write_text("", encoding="utf-8")
    with pytest.raises(UntrustedBinding):
        bound_session(spool, _THREAD)


def test_binding_a_new_thread_ends_the_sessions_earlier_binding(spool: Path) -> None:
    third = "01a193e9-8545-78d2-b7c5-4750a9514be8"
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    assert (
        consume_nonce(
            spool, _OTHER, mint_nonce(spool, _OTHER), "01a193e9-8545-78d2-b7c5-4750a9514be8"
        )
        is None
    )
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _SECOND_THREAD) is None
    assert bound_session(spool, _THREAD) is None
    assert bound_session(spool, _SECOND_THREAD) == _SESSION
    assert bound_session(spool, third) == _OTHER


@pytest.mark.parametrize("bad", ["../escape", "", "a/b", "a\nb", None])
def test_an_unsafe_id_is_refused_before_any_path_is_built(spool: Path, bad: object) -> None:
    nonce = mint_nonce(spool, _SESSION)
    assert consume_nonce(spool, bad, nonce, _THREAD) is not None
    assert consume_nonce(spool, _SESSION, nonce, bad) is not None
    assert bound_session(spool, bad) is None
    assert consume_nonce(spool, _SESSION, nonce, _THREAD) is None


def test_no_binding_directory_is_no_binding_and_creates_nothing(spool: Path) -> None:
    assert bound_session(spool, _THREAD) is None
    assert not (spool / BINDING_DIRECTORY).exists()


@pytest.mark.parametrize("level", ["", "threads"])
def test_a_directory_others_can_open_is_refused(spool: Path, level: str) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    (spool / BINDING_DIRECTORY / level).chmod(0o750)
    with pytest.raises(UntrustedBinding):
        bound_session(spool, _THREAD)
    # Refused on every path: minting must not quietly tighten what it found.
    with pytest.raises(UntrustedBinding):
        mint_nonce(spool, _OTHER)
    assert stat.S_IMODE((spool / BINDING_DIRECTORY / level).stat().st_mode) == 0o750


def test_a_linked_binding_directory_is_refused(spool: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (spool / BINDING_DIRECTORY).symlink_to(elsewhere)
    with pytest.raises(UntrustedBinding):
        bound_session(spool, _THREAD)
    with pytest.raises(UntrustedBinding):
        mint_nonce(spool, _SESSION)
    assert consume_nonce(spool, _SESSION, "0" * 64, _THREAD) is not None
    assert list(elsewhere.iterdir()) == []


def test_a_linked_thread_file_is_refused(spool: Path, tmp_path: Path) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    planted = tmp_path / "planted"
    planted.write_text(_OTHER, encoding="utf-8")
    thread_file = spool / BINDING_DIRECTORY / "threads" / _THREAD
    thread_file.unlink()
    thread_file.symlink_to(planted)
    with pytest.raises(UntrustedBinding):
        bound_session(spool, _THREAD)


@pytest.mark.parametrize("content", ["", "not a session id\n", "x" * 5000])
def test_a_thread_file_holding_no_session_is_refused(spool: Path, content: str) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    (spool / BINDING_DIRECTORY / "threads" / _THREAD).write_text(content, encoding="utf-8")
    with pytest.raises(UntrustedBinding):
        bound_session(spool, _THREAD)


def test_a_thread_file_others_can_read_is_refused(spool: Path) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    (spool / BINDING_DIRECTORY / "threads" / _THREAD).chmod(0o640)
    with pytest.raises(UntrustedBinding):
        bound_session(spool, _THREAD)


def test_prune_removes_what_belongs_to_sessions_off_the_roster(spool: Path) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    assert consume_nonce(spool, _OTHER, mint_nonce(spool, _OTHER), _SECOND_THREAD) is None
    waiting = mint_nonce(spool, "c" * 32)
    gone_waiting = "d" * 32
    mint_nonce(spool, gone_waiting)
    prune(spool, {_SESSION, "c" * 32})
    assert bound_session(spool, _THREAD) == _SESSION
    assert bound_session(spool, _SECOND_THREAD) is None
    assert consume_nonce(spool, "c" * 32, waiting, "01a193e9-8545-78d2-b7c5-4750a9514be8") is None
    assert not (spool / BINDING_DIRECTORY / "pending" / gone_waiting).exists()


def test_prune_clears_abandoned_work_files_and_keeps_fresh_ones(spool: Path) -> None:
    mint_nonce(spool, _SESSION)
    pending = spool / BINDING_DIRECTORY / "pending"
    abandoned = pending / f".claimed-{_SESSION}-x"
    fresh = pending / f".claimed-{_SESSION}-y"
    abandoned.write_text("", encoding="utf-8")
    fresh.write_text("", encoding="utf-8")
    old = time.time() - 3600
    os.utime(abandoned, (old, old))
    prune(spool, {_SESSION})
    assert not abandoned.exists()
    assert fresh.exists()


def test_prune_never_follows_a_link_and_leaves_an_untrusted_directory_alone(
    spool: Path, tmp_path: Path
) -> None:
    assert consume_nonce(spool, _SESSION, mint_nonce(spool, _SESSION), _THREAD) is None
    planted = tmp_path / "planted"
    planted.write_text(_SESSION, encoding="utf-8")
    link = spool / BINDING_DIRECTORY / "threads" / _SECOND_THREAD
    link.symlink_to(planted)
    prune(spool, {_SESSION})
    assert not link.is_symlink()
    assert planted.read_text(encoding="utf-8") == _SESSION
    (spool / BINDING_DIRECTORY).chmod(0o750)
    prune(spool, set())
    (spool / BINDING_DIRECTORY).chmod(0o700)
    assert bound_session(spool, _THREAD) == _SESSION


def test_a_failed_nonce_message_never_carries_the_nonce(spool: Path) -> None:
    mint_nonce(spool, _SESSION)
    secret = "f" * 64
    assert secret not in json.dumps(consume_nonce(spool, _SESSION, secret, _THREAD))
