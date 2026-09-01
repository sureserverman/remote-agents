"""Codex's hook configuration: which file, which events — the machinery lives in
`adapters.agents.hook_settings` and asks this value (DEC-067)."""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.agents.hook_settings import _HookProvider

PROVIDER = _HookProvider("codex", Path(".codex/hooks.json"), ("Stop", "PermissionRequest"))
