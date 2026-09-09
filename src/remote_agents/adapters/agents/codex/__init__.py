"""Codex's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.codex.remote_control import CodexRemoteControl
from remote_agents.adapters.agents.codex.sessions import (
    CodexAppServerClient,
    CodexSessionCatalogue,
)
from remote_agents.adapters.agents.codex.usage import CodexUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor, TrustDialog


def _sessions(project_paths: Mapping[ProjectId, Path]) -> CodexSessionCatalogue:
    return CodexSessionCatalogue(project_paths, CodexAppServerClient())


def descriptor() -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    Codex is the one provider wiring `remote_control`: its Remote Control is a property of
    the shared app-server daemon this machine runs, which is a host fact with no session to
    hang off. Constructed here with its default collaborators; everything below
    `tests/live` injects its own, so nothing but the live drill runs a real `codex`.
    """
    return ProviderDescriptor(
        ProfileId("codex"),
        glyph="🔷",
        sessions=_sessions,
        usage=CodexUsageReader(),
        hooks="codex",
        remote_control=CodexRemoteControl(),
        # Measured 2026-09-09 on codex-cli 0.153.4: the dialog is up 0.22 s after launch, and
        # the cursor rests on the **affirmative** -- the opposite of claude's, which is the
        # whole reason the keys are computed from the capture rather than fixed.
        #
        # `identifies_by` is NOT the question: codex and cursor-agent draw "Do you trust the
        # contents of this directory?" word for word, so it identifies neither of them.
        trust_dialog=TrustDialog(
            question="Do you trust the contents of this directory?",
            affirmative="Yes, continue",
            negative="No, quit",
            cursor="›",
            # Not the affirmative, for the reason claude's declaration gives: this is the
            # sentence codex draws under its question, and it is no answer.
            identifies_by="Working with untrusted contents",
        ),
    )
