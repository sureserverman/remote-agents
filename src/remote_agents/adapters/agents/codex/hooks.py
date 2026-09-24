"""Codex's hook configuration: which file, which events — the machinery lives in
`adapters.agents.hook_settings` and asks this value (DEC-067)."""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.agents.hook_settings import _HookProvider

#: `UserPromptSubmit` joined 2026-09-24 (BL-108, DEC-104) to start the turn marker. Codex 0.155.1
#: was measured firing it by that name, beside `SessionStart`, in a hook-event log that day; the
#: live proof against a real `codex` is `tests/live/test_codex_activity_hooks.py` (BL-108's plan,
#: Task 3.2). If a Codex build renamed it, the marker would simply never start and the relay would
#: read the screen alone, as before.
PROVIDER = _HookProvider(
    "codex", Path(".codex/hooks.json"), ("Stop", "PermissionRequest", "UserPromptSubmit")
)
