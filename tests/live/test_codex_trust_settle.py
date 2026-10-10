"""Opt-in re-measure of Codex's trust settle: 30 launches into fresh git repos (DEC-081 (b)).

DEC-081 lets a pane read clear `untrusted` but never set it, which leaves the launch settle as
the only thing that ever records it. That is safe only while the settle reliably outlasts the
gap between Codex's banner and its folder-trust dialog, so the decision made the correctors'
removal conditional on measuring that gap over many launches rather than the five of 2026-09-28.

Codex 0.162.0 asks only inside a git repository (measured 2026-10-09), so every launch gets its
own freshly initialised repo. Nothing is ever answered: the dialog is read and the server is
killed, so no folder is trusted and no model quota is spent.

Set `REMOTE_AGENTS_SETTLE_REPORT` to a path to have the per-launch gaps written there as JSON;
`docs/acceptance-2026-10-09-codex-trust-settle.md` was written from that file.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import profile_readiness_blockers
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles

LAUNCHES = 30
BANNER = "Codex"
DIALOG = "Folder access"
POLL_SECONDS = 0.01


def _fresh_repo(path: Path) -> Path:
    path.mkdir()
    git = ("git", "-C", str(path))
    subprocess.run((*git, "init", "-q"), check=True)
    subprocess.run(
        (
            *git,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=settle",
            "-c",
            "user.email=settle@invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ),
        check=True,
    )
    return path.resolve()


async def _sample(gateway: TmuxGateway, session_id: SessionId) -> tuple[float | None, float | None]:
    """Poll the pane every 10 ms; return when the banner and when the dialog first showed."""
    loop = asyncio.get_running_loop()
    banner: float | None = None
    deadline = loop.time() + 30.0
    while loop.time() < deadline:
        try:
            capture = await gateway.capture(session_id)
        except (RuntimeError, LookupError):
            capture = ""
        now = loop.time()
        if banner is None and BANNER in capture:
            banner = now
        if DIALOG in capture:
            return banner if banner is not None else now, now
        await asyncio.sleep(POLL_SECONDS)
    return banner, None


@pytest.mark.live_profile
async def test_codex_trust_settle_catches_the_dialog_on_every_launch(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    if "codex" not in request.config.getoption("profile"):
        pytest.skip("profile was not selected")
    definition = next(p for p in closed_profiles() if str(p.profile_id) == "codex")
    executable = shutil.which(definition.executable)
    if executable is None:
        pytest.skip("BLOCKED: executable_missing")
    environment = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
        if key in os.environ
    }
    version = subprocess.run(
        (executable, "--version"), capture_output=True, text=True, check=True
    ).stdout.strip()
    rows: list[dict[str, object]] = []
    for index in range(LAUNCHES):
        session_id = SessionId.new()
        project_id = ProjectId("settle")
        project = _fresh_repo(tmp_path / f"repo-{index:02d}")
        profile = build_launch_profile(
            definition,
            Path(executable),
            session_id,
            environment,
            trust_blockers=profile_readiness_blockers().get("codex", ()),
        )
        socket = f"remote-agents-test-{session_id.value.hex}"
        # A held 160x40 session sizes the server's later sessions, as in the 0.158 drill.
        subprocess.run(
            ("tmux", "-L", socket, "-f", "/dev/null", "new-session", "-d", "-s", "hold")
            + ("-x", "160", "-y", "40", ";", "set-option", "-g", "default-size", "160x40"),
            check=True,
        )
        sampler: asyncio.Task[tuple[float | None, float | None]] | None = None
        try:
            gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
            terminal = TmuxTerminal(
                gateway, {project_id: project}, {definition.profile_id: profile}, startup_timeout=20
            )
            sampler = asyncio.create_task(_sample(gateway, session_id))
            launched = await terminal.launch(session_id, project_id, definition.profile_id)
            banner, dialog = await sampler
        finally:
            if sampler is not None and not sampler.done():
                sampler.cancel()
            subprocess.run(("tmux", "-L", socket, "kill-server"), check=False)
        gap = None if banner is None or dialog is None else round(dialog - banner, 3)
        rows.append(
            {
                "launch": index + 1,
                "awaiting_trust": launched.awaiting_trust,
                "live": launched.live,
                "gap_seconds": gap,
            }
        )
    settle = profile.trust_settle_seconds
    gaps = [float(row["gap_seconds"]) for row in rows if row["gap_seconds"] is not None]
    report = os.environ.get("REMOTE_AGENTS_SETTLE_REPORT")
    if report:
        Path(report).write_text(
            json.dumps(
                {"codex_version": version, "settle_seconds": settle, "launches": rows},
                indent=2,
            )
            + "\n"
        )
    assert all(row["awaiting_trust"] is True for row in rows), rows
    assert len(gaps) == LAUNCHES, rows
    assert max(gaps) < settle, (max(gaps), settle)
