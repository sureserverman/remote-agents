"""Cursor's `stop` hook is installed in Cursor's own shape, reversibly, beside the owner's hooks.

Cursor's `hooks.json` holds flat entries under each event (`{"command": ...}`), not Claude's
groups, and carries `"version": 1`. The user-level file is the one remote-agents writes:
`docs/acceptance-2026-10-08-cursor-user-stop-hook.md` measured cursor-agent reading it and the
hook inheriting `REMOTE_AGENTS_SESSION_ID`.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import (
    HookInstallError,
    default_settings_path,
    install_agent_hooks,
    remove_agent_hooks,
)
from remote_agents.bootstrap import main

#: The owner's real file on the measuring host, as it stood on 2026-10-09: the planning
#: plugin's own `stop` and `beforeShellExecution` entries, which must survive every write.
_FOREIGN = {
    "version": 1,
    "hooks": {
        "stop": [
            {
                "type": "command",
                "command": "/home/user/.cursor/plugins/local/planning/hooks/plan-continue.sh",
                "timeout": 10,
                "loop_limit": 25,
            }
        ],
        "beforeShellExecution": [
            {
                "type": "command",
                "command": "/home/user/.cursor/plugins/local/planning/hooks/commit-gate.sh",
                "timeout": 30,
            }
        ],
    },
}


def _hooks_file(directory: Path, document: object = _FOREIGN) -> Path:
    path = directory / "hooks.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_cursor_writes_its_own_shape_beside_a_foreign_stop_and_restores_bytes(
    tmp_path: Path,
) -> None:
    path = _hooks_file(tmp_path)
    before = path.read_bytes()

    install_agent_hooks(path, executable=Path("/old/python"), provider="cursor")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert set(document["hooks"]) == {"stop", "beforeShellExecution"}
    foreign, ours = document["hooks"]["stop"]
    assert foreign == _FOREIGN["hooks"]["stop"][0]
    assert ours == {"command": "/old/python -m remote_agents agent-event --provider cursor"}
    assert document["hooks"]["beforeShellExecution"] == _FOREIGN["hooks"]["beforeShellExecution"]

    install_agent_hooks(path, executable=Path("/new/python"), provider="cursor")
    text = path.read_text(encoding="utf-8")
    assert text.count("/new/python") == 1
    assert "/old/python" not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    remove_agent_hooks(path, provider="cursor")
    assert path.read_bytes() == before, "--remove takes out only its own entry"


def test_remove_leaves_the_foreign_entry_and_the_file(tmp_path: Path) -> None:
    path = _hooks_file(tmp_path)
    install_agent_hooks(path, executable=Path("/usr/bin/python3"), provider="cursor")

    outcome = remove_agent_hooks(path, provider="cursor")

    assert outcome.changed
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == _FOREIGN


def test_a_fresh_file_carries_cursors_version(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"

    install_agent_hooks(path, executable=Path("/usr/bin/python3"), provider="cursor")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "hooks": {
            "stop": [{"command": "/usr/bin/python3 -m remote_agents agent-event --provider cursor"}]
        },
    }

    remove_agent_hooks(path, provider="cursor")
    assert path.is_file(), "a removal rewrites the file; it never deletes it"
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1}


def test_a_foreign_entry_naming_our_command_with_a_timeout_is_not_ours(tmp_path: Path) -> None:
    """An entry carrying any key but `command` is one this installer did not write."""
    command = "/usr/bin/python3 -m remote_agents agent-event --provider cursor"
    document = {"version": 1, "hooks": {"stop": [{"command": command, "timeout": 5}]}}
    path = _hooks_file(tmp_path, document)

    remove_agent_hooks(path, provider="cursor")

    assert json.loads(path.read_text(encoding="utf-8")) == document


def test_cursor_default_and_cli_provider(tmp_path: Path) -> None:
    assert default_settings_path(tmp_path, provider="cursor") == tmp_path / ".cursor" / "hooks.json"
    path = _hooks_file(tmp_path)

    assert main(["install-agent-hooks", "--provider", "cursor", "--settings", str(path)]) == 0
    stops = json.loads(path.read_text(encoding="utf-8"))["hooks"]["stop"]
    assert stops[-1]["command"].endswith("-m remote_agents agent-event --provider cursor")

    assert (
        main(["install-agent-hooks", "--provider", "cursor", "--settings", str(path), "--remove"])
        == 0
    )
    assert json.loads(path.read_text(encoding="utf-8")) == _FOREIGN


def test_the_cli_accepts_exactly_the_registrys_installers() -> None:
    """One name list for `agent-event` and `install-agent-hooks`, held to the installers."""
    from remote_agents.adapters.agents.registry import _PROVIDERS
    from remote_agents.agent_event import HOOK_PROVIDERS

    assert set(HOOK_PROVIDERS) == set(_PROVIDERS)
    assert "cursor" in _PROVIDERS


@pytest.mark.parametrize(
    "document",
    [{"version": 1, "hooks": {}}, {"version": 1, "hooks": {"stop": None}}],
    ids=["empty-hooks", "null-stop"],
)
def test_an_empty_container_is_refused_and_left_untouched(tmp_path: Path, document) -> None:
    """Removal could not tell `{}` or a null from no key at all, so install refuses first."""
    path = _hooks_file(tmp_path, document)
    before = path.read_bytes()

    with pytest.raises(HookInstallError, match="left untouched"):
        install_agent_hooks(path, executable=Path("/usr/bin/python3"), provider="cursor")

    assert path.read_bytes() == before
