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


@pytest.mark.parametrize("defect", [None, "path", "preset", "provider", "extra", "duplicate"])
def test_only_the_exact_curated_full_access_constant_is_allowed(tmp_path, defect):
    relative = "src/remote_agents/domain/profiles.py" if defect != "path" else "src/remote.py"
    preset = "ASK" if defect == "preset" else "FULL_ACCESS"
    provider = "claude" if defect == "provider" else "codex"
    flag = '"--dangerously-bypass-approvals-and-sandbox"'
    value = f"({flag},)" if defect != "extra" else f'({flag}, "--extra")'
    content = (
        '_PERMISSION_ARGUMENTS = {"'
        + provider
        + '": {PermissionPreset.'
        + preset
        + ": "
        + value
        + "}}\n"
    )
    if defect == "duplicate":
        content += f"extra = {flag}\n"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(content)
    findings = scan(tmp_path)
    assert bool(findings) is (defect is not None)
