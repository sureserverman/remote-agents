"""Cursor's hook configuration: which file, which event, which shape — the machinery lives in
`adapters.agents.hook_settings` and asks this value (DEC-067).

The user-level `~/.cursor/hooks.json`, which cursor-agent 2026.10.01-e373342 was measured reading,
with its `stop` hook inheriting the pane's `REMOTE_AGENTS_SESSION_ID`
(`docs/acceptance-2026-10-08-cursor-user-stop-hook.md`). One event: `stop`, which the spool admits
only when its `status` is `completed`. Cursor's entries sit directly under the event and its file
carries `"version": 1`, so the value says `flat` and seeds a fresh file with the version.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from remote_agents.adapters.agents.hook_settings import _HookProvider
from remote_agents.ports.provider_descriptor import EventLicence, HookLicence

PROVIDER = _HookProvider(
    "cursor", Path(".cursor/hooks.json"), ("stop",), flat=True, fresh=(("version", 1),)
)


def _completed(document: Mapping[str, object]) -> bool:
    """An `aborted` or `error` stop is not a turn that finished, so it records nothing."""
    return document.get("status") == "completed"


HOOK_LICENCE = HookLicence(
    # The event, the session and the time, and no field of the payload: Cursor's `stop` carries
    # `user_email`, `transcript_path` and `workspace_roots`, and none of them is read (DEC-013,
    # DEC-037; `docs/acceptance-2026-10-08-cursor-user-stop-hook.md`).
    events=MappingProxyType({"stop": EventLicence()}),
    starts_turn_marker=False,
    admits=_completed,
)
"""What Cursor's activity hook may record (BL-050); the descriptor's `activity` is this value."""
