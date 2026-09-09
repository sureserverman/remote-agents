"""Cursor's provider vertical: sessions, usage, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.cursor.sessions import CursorSessionCatalogue
from remote_agents.adapters.agents.cursor.usage import CursorUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor, TrustDialog


def _sessions(project_paths: Mapping[ProjectId, Path]) -> CursorSessionCatalogue:  # noqa: ARG001
    # Cursor's catalogue is workspace-blind; the parameter keeps the factory contract one
    # shape for every provider.
    return CursorSessionCatalogue()


def descriptor() -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    Hooks and `remote_control` both stay a declared None. `usage` is constant-empty and
    deliberately NOT None: cursor answers "I publish nothing", which renders as "not
    reported by this agent"; a None here
    would render "no conversation matched yet" forever (DEC-061 — the two must never
    conflate; the fold regression test pins the consequence).
    """
    return ProviderDescriptor(
        ProfileId("cursor-agent"),
        glyph="🔶",
        sessions=_sessions,
        usage=CursorUsageReader(),
        # Measured 2026-09-09 on 2026.09.08-6caf4ff: up 0.66 s after launch, cursor on the
        # **affirmative**, and drawn **inside a box** -- so the `▶` is not the first character
        # of its row (`│` is) and the directory path wraps across two rows. The parser looks
        # for the glyph anywhere on a row for exactly this dialog.
        #
        # It also offers direct keys (`[a]`, `[q]`) and says so on screen. This project does
        # not use them: arrows-and-Enter is the one route identical across all three dialogs,
        # and a single letter sent into the wrong screen types a letter.
        trust_dialog=TrustDialog(
            question="Do you trust the contents of this directory?",
            affirmative="Trust this workspace",
            negative="Quit",
            cursor="▶",
            identifies_by="Workspace Trust Required",
        ),
    )
