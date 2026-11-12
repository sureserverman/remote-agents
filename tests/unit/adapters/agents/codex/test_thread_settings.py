"""Codex's preset table, and the descriptor that publishes it (Task 1.2)."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from remote_agents.adapters.agents.codex.thread_settings import (
    CODEX_THREAD_SETTINGS,
    CodexThreadPermissions,
)
from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.domain.profiles import PermissionPreset

#: The exact params each preset sends. Pinned in full: this is a security policy, and the
#: approve-for-me row is the Codex CLI's own expansion of its flag (codex-rs/cli/src/main.rs).
EXPECTED = {
    PermissionPreset.APPROVE_FOR_ME: {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "auto_review",
        "sandboxPolicy": {"type": "workspaceWrite"},
    },
    PermissionPreset.ASK: {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "workspaceWrite"},
    },
    PermissionPreset.FULL_ACCESS: {
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "dangerFullAccess"},
    },
}


def _plain(value):
    if isinstance(value, MappingProxyType | dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def test_the_table_sets_exactly_the_pinned_params_per_preset() -> None:
    assert {preset: _plain(params) for preset, params in CODEX_THREAD_SETTINGS.items()} == EXPECTED


def test_the_table_has_no_row_for_the_provider_default() -> None:
    assert PermissionPreset.PROVIDER_DEFAULT not in CODEX_THREAD_SETTINGS


def test_the_table_cannot_be_edited_at_runtime() -> None:
    with pytest.raises(TypeError):
        CODEX_THREAD_SETTINGS[PermissionPreset.ASK] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        CODEX_THREAD_SETTINGS[PermissionPreset.ASK]["approvalPolicy"] = "never"  # type: ignore[index]


def test_the_table_is_published_only_by_the_codex_descriptor() -> None:
    declared = {
        str(descriptor.profile_id): descriptor.thread_permissions
        for descriptor in provider_descriptors()
    }
    assert isinstance(declared.pop("codex"), CodexThreadPermissions)
    assert set(declared.values()) == {None}
