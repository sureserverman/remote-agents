"""Opt-in proof that a real Codex turn runs only the temporary hook configuration.

The hook file, hook trust, and all writable Codex state live under a disposable project-local
``.codex`` directory within this test's temporary workspace. They therefore neither read nor
write the owner's ``~/.codex/hooks.json``, persisted hook trust, or the service's production
spool. ``--dangerously-bypass-hook-trust`` is constrained to this vetted, generated hook
definition; it is never a direction to trust the owner's configuration.

Codex reads only the owner's existing ``auth.json`` through a temporary owner-only symlink.
The test never opens, copies, serializes, logs, or modifies that credential; tracing the
installed CLI confirmed it opens the file read-only. This preserves the ordinary ChatGPT
Codex entitlement instead of requiring unrelated API billing.
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
from remote_agents.ports.agent_activity import MAXIMUM_DETAIL_CHARACTERS, ActivityKind
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
    _link_chatgpt_auth(codex_home)
    install_agent_hooks(
        codex_home / "hooks.json",
        executable=Path(sys.executable),
        activity_directory=spool,
        provider="codex",
    )
    return workspace, spool


def _link_chatgpt_auth(codex_home: Path) -> None:
    source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    source_auth = source_home / "auth.json"
    if not source_auth.is_file():
        pytest.skip("BLOCKED: Codex is not logged in with ChatGPT")
    os.symlink(source_auth, codex_home / "auth.json")


def _run_codex(workspace: Path, environment: dict[str, str]) -> None:
    runtime_environment = {**os.environ, "CODEX_HOME": str(workspace / ".codex")}
    # This pytest process can itself be a managed Codex turn. Its identity belongs to the
    # parent, not the disposable child used for the negative proof, so make inheritance an
    # explicit opt-in through ``environment`` rather than an accidental second managed turn.
    runtime_environment.pop(SESSION_ID_VARIABLE, None)
    runtime_environment.update(environment)
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
        env=runtime_environment,
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
    assert len(activities) == 1, "a managed Codex turn must spool exactly one Stop activity"
    (activity,) = activities
    assert activity.session_id == str(session_id)
    assert activity.kind is ActivityKind.COMPLETED
    # `detail is None` until 2026-08-30, when the Codex branch of `_observed_event` stopped
    # discarding every payload field and began reading the one `last_assistant_message` that
    # `docs/acceptance-2026-08-29-codex-activity-detail.md` measured. `_TURN` asks for one exact
    # word, so this is the agent's own line arriving through a real hook rather than a fixture --
    # which is the whole reason this drill exists (DEC-013: "what prevents a recurrence is a live
    # drill, not another fixture"). Asserted on the *content*, not merely on not-None: a bound
    # that silently truncated to the empty string would satisfy the weaker check.
    assert activity.detail is not None, (
        "a managed Codex Stop now carries the agent's last line; if this is None the spool's "
        "codex branch has stopped reading `last_assistant_message`"
    )
    assert "spooled" in activity.detail.casefold()
    assert len(activity.detail) <= MAXIMUM_DETAIL_CHARACTERS


@pytest.mark.live_profile
def test_an_unmanaged_codex_turn_spools_nothing(tmp_path: Path) -> None:
    workspace, spool = _requirements(tmp_path)

    _run_codex(workspace, {})

    assert drain_activity(spool) == ()
    assert not spool.exists() or list(spool.iterdir()) == []
