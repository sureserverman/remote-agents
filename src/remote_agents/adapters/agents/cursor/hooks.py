"""Cursor's hook configuration: which file, which event, which shape — the machinery lives in
`adapters.agents.hook_settings` and asks this value (DEC-067).

The user-level `~/.cursor/hooks.json`, which cursor-agent 2026.10.01-e373342 was measured reading,
with its `stop` hook inheriting the pane's `REMOTE_AGENTS_SESSION_ID`
(`docs/acceptance-2026-10-08-cursor-user-stop-hook.md`). One event: `stop`, which the spool admits
only when its `status` is `completed`. Cursor's entries sit directly under the event and its file
carries `"version": 1`, so the value says `flat` and seeds a fresh file with the version.
"""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.agents.hook_settings import _HookProvider

PROVIDER = _HookProvider(
    "cursor", Path(".cursor/hooks.json"), ("stop",), flat=True, fresh=(("version", 1),)
)
