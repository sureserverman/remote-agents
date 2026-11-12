"""Apply a permission preset to a Codex thread on the shared app-server daemon.

**Why after launch, not at it.** Codex 0.167 runs a TUI started with any command-line config
override -- the permission flags included -- on an embedded app server, off the shared daemon
(`codex-rs/tui/src/daemon_startup.rs`, `exclusion()`), and Codex Remote Control reaches only
threads on that daemon. So a managed Codex launches plain, and the service sets the thread's
permissions here once it knows which thread is the session's (DEC-138).

**What a preset means to Codex**, measured on 0.167.2 against the CLI's own expansion of its
approve-for-me flag (`codex-rs/cli/src/main.rs` tests): approval policy, approval reviewer and
sandbox. `thread/settings/update` takes the three, and a TUI attached to the daemon applies the
`ThreadSettingsUpdated` notification to its active thread (`chatwidget/settings.rs`). The
provider default has no row: it means "add nothing", and the caller sends nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.thread_permissions import ThreadPermissionsError

_WORKSPACE = MappingProxyType({"type": "workspaceWrite"})

#: Preset -> the `thread/settings/update` params it sets, besides `threadId`. Closed, and pinned
#: by an exact-match test: these are a security policy, so a value is never assembled from input.
CODEX_THREAD_SETTINGS: Mapping[PermissionPreset, Mapping[str, object]] = MappingProxyType(
    {
        PermissionPreset.APPROVE_FOR_ME: MappingProxyType(
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
                "sandboxPolicy": _WORKSPACE,
            }
        ),
        PermissionPreset.ASK: MappingProxyType(
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandboxPolicy": _WORKSPACE,
            }
        ),
        PermissionPreset.FULL_ACCESS: MappingProxyType(
            {
                "approvalPolicy": "never",
                "sandboxPolicy": MappingProxyType({"type": "dangerFullAccess"}),
            }
        ),
    }
)


class CodexThreadPermissions:
    """The `ThreadPermissions` port over Codex's shared app-server daemon."""

    async def apply(self, thread_id: str, preset: PermissionPreset) -> None:
        raise ThreadPermissionsError("not implemented")
