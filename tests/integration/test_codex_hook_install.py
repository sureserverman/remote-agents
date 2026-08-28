"""Codex hooks are reversible and never merge into Claude's configuration."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from remote_agents.adapters.agents.hook_install import (
    HookInstallError,
    default_settings_path,
    install_agent_hooks,
    remove_agent_hooks,
)
from remote_agents.bootstrap import main


def _hooks_file(directory: Path, document: object | None = None) -> Path:
    path = directory / "hooks.json"
    if isinstance(document, bytes):
        path.write_bytes(document)
        return path
    content = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo foreign"}]}]}}
    path.write_text(
        json.dumps(content if document is None else document, indent=2) + "\n", encoding="utf-8"
    )
    return path


def test_codex_installs_only_stop_and_permission_request_and_restores_bytes(tmp_path: Path) -> None:
    path = _hooks_file(tmp_path)
    before = path.read_bytes()
    path.chmod(0o600)

    install_agent_hooks(path, executable=Path("/old/python"), provider="codex")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document["hooks"]) == {"Stop", "PermissionRequest"}
    assert document["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo foreign"
    assert "--provider codex" in document["hooks"]["Stop"][1]["hooks"][0]["command"]

    install_agent_hooks(path, executable=Path("/new/python"), provider="codex")
    assert path.read_text(encoding="utf-8").count("/new/python") == 2
    assert "/old/python" not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    remove_agent_hooks(path, provider="codex")
    assert path.read_bytes() == before


def test_codex_default_and_cli_provider_are_explicit(tmp_path: Path) -> None:
    assert default_settings_path(tmp_path, provider="codex") == tmp_path / ".codex" / "hooks.json"
    path = _hooks_file(tmp_path)

    assert main(["install-agent-hooks", "--provider", "codex", "--settings", str(path)]) == 0
    assert set(json.loads(path.read_text(encoding="utf-8"))["hooks"]) == {
        "Stop",
        "PermissionRequest",
    }


def test_codex_refuses_invalid_or_concurrently_changed_config(tmp_path: Path, monkeypatch) -> None:
    path = _hooks_file(tmp_path, b"not json")
    with pytest.raises(HookInstallError):
        install_agent_hooks(path, provider="codex")

    path = _hooks_file(tmp_path)
    monkeypatch.setattr(
        "remote_agents.adapters.agents.hook_install._refuse_if_changed_since_it_was_read",
        lambda *args: (_ for _ in ()).throw(HookInstallError("changed")),
    )
    with pytest.raises(HookInstallError, match="changed"):
        install_agent_hooks(path, provider="codex")
