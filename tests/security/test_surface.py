"""Fixtures proving the prohibited-control-surface sweep fails for each defect class."""

from __future__ import annotations

from pathlib import Path

import pytest
from check_surface import scan


@pytest.mark.parametrize(
    ("relative_path", "content", "expected"),
    (
        (
            "src/remote_agents/remote.py",
            "def send_keystroke(): pass\n",
            "prohibited remote surface",
        ),
        (
            "src/remote_agents/remote.py",
            "import subprocess\nsubprocess.run('x', shell=True)\n",
            "shell subprocess invocation",
        ),
        ("src/remote_agents/remote.py", "import os\nos.system('x')\n", "legacy shell invocation"),
        (
            "src/remote_agents/remote.py",
            "import asyncio\nasyncio.create_subprocess_shell('x')\n",
            "async shell invocation",
        ),
        ("config/telegram.env", "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=secret\n", "secret literal"),
        ("config/config.toml", "bot_token = 'secret'\n", "secret literal"),
        ("systemd/remote-agents.service", "ExecStart=x --yolo\n", "prohibited remote surface"),
    ),
)
def test_surface_sweep_rejects_every_prohibited_defect_class(
    tmp_path, relative_path: str, content: str, expected: str
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")

    findings = scan(tmp_path)

    assert any(expected in finding.reason for finding in findings)


def test_current_repository_exposes_only_the_approved_remote_surface() -> None:
    assert scan(Path(__file__).parents[2]) == ()


def test_no_permission_bypass_literal_is_allowed_even_in_the_profiles_module(tmp_path):
    """The one carve-out for a Full access launch flag left with the flag (v0.70.0).

    No preset is a launch argument any more, so `domain/profiles.py` is scanned like
    everything else.
    """
    path = tmp_path / "src/remote_agents/domain/profiles.py"
    path.parent.mkdir(parents=True)
    path.write_text('FLAGS = {"codex": ("--dangerously-bypass-approvals-and-sandbox",)}\n')
    assert any("prohibited remote surface" in finding.reason for finding in scan(tmp_path))
