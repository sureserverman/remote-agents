"""Opt-in proof that a real Codex turn runs only the temporary hook configuration.

The Codex hook file and temporary API-key login live under a disposable project-local
``.codex`` directory within this test's temporary workspace.  They therefore neither read nor
write the owner's ``~/.codex/hooks.json``, persisted hook trust, or the service's production
spool. ``--dangerously-bypass-hook-trust`` is constrained to this vetted, generated hook
definition; it is never a direction to trust the owner's configuration.

An owner supplies ``OPENAI_API_KEY`` only to the process that launches this opt-in test. The
key goes straight to ``codex login --with-api-key`` on standard input and is never written by
this test, included in a command line, logged, or asserted. Missing API access is ``BLOCKED``;
the owner's interactive ChatGPT login cannot be reused without reading its global hook file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from remote_agents.adapters.agents.hook_install import install_agent_hooks
from remote_agents.application.activity import drain_activity
from remote_agents.domain.models import SessionId
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

_TURN = "Reply with exactly the word: spooled"
_BLOCKED_OUTPUT = {
    "authentication": ("login", "auth", "unauthorized", "api key"),
    "network": ("network", "connection"),
    "quota or billing": ("rate limit", "usage limit", "billing", "credits"),
}


def _requirements(tmp_path: Path) -> tuple[Path, Path]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    if shutil.which("codex") is None:
        pytest.skip("BLOCKED: executable_missing")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = workspace / ".codex"
    codex_home.mkdir(mode=0o700)
    spool = tmp_path / "activity"
    _login_isolated(codex_home)
    install_agent_hooks(
        codex_home / "hooks.json",
        executable=Path(sys.executable),
        activity_directory=spool,
        provider="codex",
    )
    return workspace, spool


def _login_isolated(codex_home: Path) -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("BLOCKED: isolated Codex qualification requires OPENAI_API_KEY")
    completed = subprocess.run(
        ["codex", "login", "--with-api-key"],
        input=api_key,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )
    if completed.returncode != 0:
        pytest.skip("BLOCKED: Codex did not accept the isolated API-key login")


def _run_codex(workspace: Path, environment: dict[str, str]) -> None:
    completed = subprocess.run(
        [
            "codex",
            "exec",
            "--dangerously-bypass-hook-trust",
            "--skip-git-repo-check",
            "--ephemeral",
            "--cd",
            str(workspace),
            _TURN,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=workspace,
        env={**os.environ, **environment, "CODEX_HOME": str(workspace / ".codex")},
    )
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    if completed.returncode != 0:
        blocked_reason = next(
            (
                reason
                for reason, markers in _BLOCKED_OUTPUT.items()
                if any(marker in output for marker in markers)
            ),
            None,
        )
        if blocked_reason is not None:
            pytest.skip(f"BLOCKED: Codex {blocked_reason} is unavailable for the live drill")
    assert completed.returncode == 0, completed.stderr


@pytest.mark.live_profile
def test_a_managed_codex_turn_spools_its_own_stop(tmp_path: Path) -> None:
    workspace, spool = _requirements(tmp_path)
    session_id = SessionId.new()

    _run_codex(workspace, {SESSION_ID_VARIABLE: str(session_id)})

    activities = drain_activity(spool)
    assert activities, "a managed Codex session's Stop hook spooled nothing"
    assert {activity.session_id for activity in activities} == {str(session_id)}
    assert ActivityKind.COMPLETED in {activity.kind for activity in activities}


@pytest.mark.live_profile
def test_an_unmanaged_codex_turn_spools_nothing(tmp_path: Path) -> None:
    workspace, spool = _requirements(tmp_path)

    _run_codex(workspace, {})

    assert drain_activity(spool) == ()
    assert not spool.exists() or list(spool.iterdir()) == []
