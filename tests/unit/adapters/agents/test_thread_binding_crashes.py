"""Real process deaths cannot erase a launch's binding recovery state."""

from __future__ import annotations

import os
import time

import pytest
from binding_crash import crash_bind

from remote_agents.adapters.agents.thread_binding import (
    BINDING_DIRECTORY,
    UntrustedBinding,
    bound_session,
    consume_nonce,
    mint_nonce,
    pending_state,
    prune,
)
from remote_agents.ports.thread_binding import PendingBinding

SESSION = "a" * 32
THREAD = "01a193e7-e8c8-7272-9c5f-6ee9e7749464"


@pytest.mark.parametrize("phase", ["claim", "publish"])
@pytest.mark.parametrize("retry", [False, True])
def test_a_crash_keeps_the_exact_attempt_even_after_pruning(tmp_path, phase, retry):
    nonce = mint_nonce(tmp_path, SESSION, retry=retry)
    crash_bind(tmp_path, SESSION, nonce, THREAD, phase)
    pending = tmp_path / BINDING_DIRECTORY / "pending"
    for path in pending.iterdir():
        os.utime(path, (time.time() - 3600,) * 2)
    prune(tmp_path, {SESSION})
    assert pending_state(tmp_path, SESSION) is (
        PendingBinding.RETRIED if retry else PendingBinding.TRIED
    )
    assert bound_session(tmp_path, THREAD) is None
    # A claimed nonce is spent, including when its publisher died.
    assert consume_nonce(tmp_path, SESSION, nonce, THREAD) is not None
    fresh = mint_nonce(tmp_path, SESSION, retry=True)
    assert consume_nonce(tmp_path, SESSION, nonce, THREAD) is not None
    assert consume_nonce(tmp_path, SESSION, fresh, THREAD) is None


def test_a_crash_after_publication_keeps_the_binding_and_refuses_replay(tmp_path):
    nonce = mint_nonce(tmp_path, SESSION)
    crash_bind(tmp_path, SESSION, nonce, THREAD, "published")
    prune(tmp_path, {SESSION})
    assert bound_session(tmp_path, THREAD) == SESSION
    assert consume_nonce(tmp_path, SESSION, nonce, THREAD) is not None


@pytest.mark.parametrize("malformed", [False, True])
def test_ambiguous_or_malformed_claims_refuse_instead_of_choosing(tmp_path, malformed):
    mint_nonce(tmp_path, SESSION)
    pending = tmp_path / BINDING_DIRECTORY / "pending"
    claimed = pending / f".claimed-{SESSION}-{'1' * 16}"
    (pending / SESSION).rename(claimed)
    if malformed:
        claimed.write_text("not a nonce hash")
    else:
        other = pending / f".claimed-{SESSION}-{'2' * 16}"
        other.write_bytes(claimed.read_bytes())
        other.chmod(0o600)
    prune(tmp_path, {SESSION})
    with pytest.raises(UntrustedBinding):
        pending_state(tmp_path, SESSION)


@pytest.mark.parametrize("defect", ["symlink", "public", "hardlink"])
def test_the_transaction_lock_is_private_and_never_repaired(tmp_path, defect):
    nonce = mint_nonce(tmp_path, SESSION)
    lock = tmp_path / BINDING_DIRECTORY / ".lock"
    lock.unlink(missing_ok=True)
    outside = tmp_path / "untouched"
    outside.write_text("unchanged")
    if defect == "symlink":
        lock.symlink_to(outside)
    elif defect == "hardlink":
        os.link(outside, lock)
    else:
        lock.write_text("")
        lock.chmod(0o644)
    with pytest.raises(UntrustedBinding):
        mint_nonce(tmp_path, SESSION)
    assert consume_nonce(tmp_path, SESSION, nonce, THREAD) is not None
    assert outside.read_text() == "unchanged"
    assert bound_session(tmp_path, THREAD) is None


def test_replacement_waits_for_the_inflight_publisher_across_processes(tmp_path):
    import json
    import subprocess
    import sys

    old = mint_nonce(tmp_path, SESSION)
    entered, release, replacing = (tmp_path / name for name in ("entered", "release", "replacing"))
    publisher_code = """import json,sys,time
from pathlib import Path
from remote_agents.adapters.agents import thread_binding as binding
p=json.load(sys.stdin)
publish=binding._link_new
def held(*args):
    Path(p['entered']).touch()
    while not Path(p['release']).exists(): time.sleep(0.01)
    return publish(*args)
binding._link_new=held
assert binding.consume_nonce(Path(p['spool']),p['session'],p['nonce'],p['thread']) is None
"""
    replacement_code = """import json,sys
from pathlib import Path
from remote_agents.adapters.agents.thread_binding import mint_nonce
p=json.load(sys.stdin)
Path(p['replacing']).touch()
print(mint_nonce(Path(p['spool']),p['session'],retry=True))
"""
    payload = {
        "spool": str(tmp_path),
        "session": SESSION,
        "nonce": old,
        "thread": THREAD,
        "entered": str(entered),
        "release": str(release),
        "replacing": str(replacing),
    }
    children = []

    def start(code):
        child = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        children.append(child)
        child.stdin.write(json.dumps(payload))
        child.stdin.close()
        child.stdin = None
        return child

    def reached(path, child):
        deadline = time.monotonic() + 5
        while not path.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.exists(), child.communicate(timeout=1)

    try:
        publisher = start(publisher_code)
        reached(entered, publisher)
        replacement = start(replacement_code)
        reached(replacing, replacement)
        with pytest.raises(subprocess.TimeoutExpired):
            replacement.wait(timeout=0.2)
        release.touch()
        _, error = publisher.communicate(timeout=5)
        assert publisher.returncode == 0, error
        fresh, error = replacement.communicate(timeout=5)
        assert replacement.returncode == 0, error
        assert bound_session(tmp_path, THREAD) is None, "replacement must revoke the old binding"
        assert consume_nonce(tmp_path, SESSION, old, THREAD) is not None
        assert consume_nonce(tmp_path, SESSION, fresh.strip(), THREAD) is None
    finally:
        release.touch()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("action", ["mint", "request", "give_up", "consume"])
@pytest.mark.parametrize("defect", ["multiple", "malformed", "public"])
def test_mutation_preserves_untrusted_claims_before_touching_existing_records(
    tmp_path, action, defect
):
    from remote_agents.adapters.agents.thread_binding import give_up, request_binding

    nonce = mint_nonce(tmp_path, SESSION)
    pending = tmp_path / BINDING_DIRECTORY / "pending"
    claim = pending / f".claimed-{SESSION}-{'1' * 16}"
    claim.write_bytes((pending / SESSION).read_bytes())
    claim.chmod(0o600)
    if defect == "multiple":
        other = pending / f".claimed-{SESSION}-{'2' * 16}"
        other.write_bytes(claim.read_bytes())
        other.chmod(0o600)
    elif defect == "malformed":
        claim.write_text("not a nonce hash")
    else:
        claim.chmod(0o644)
    # Valid published evidence must not be erased by a refused mint either.
    thread = tmp_path / BINDING_DIRECTORY / "threads" / THREAD
    thread.write_text(SESSION)
    thread.chmod(0o600)
    before = {path.name: path.read_bytes() for path in pending.iterdir()}
    if action == "consume":
        assert consume_nonce(tmp_path, SESSION, nonce, "other-thread") is not None
    else:
        operation = {"mint": mint_nonce, "request": request_binding, "give_up": give_up}[action]
        with pytest.raises(UntrustedBinding):
            operation(tmp_path, SESSION)
    assert {path.name: path.read_bytes() for path in pending.iterdir()} == before
    assert bound_session(tmp_path, THREAD) == SESSION
    prune(tmp_path, {SESSION})
    assert {path.name: path.read_bytes() for path in pending.iterdir()} == before
