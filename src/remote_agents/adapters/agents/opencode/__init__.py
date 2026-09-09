"""OpenCode's provider vertical: sessions, usage, its activity plugin, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.opencode.sessions import (
    OpenCodeCliRunner,
    OpenCodeSessionCatalogue,
)
from remote_agents.adapters.agents.opencode.usage import OpenCodeUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor


def _sessions(project_paths: Mapping[ProjectId, Path]) -> OpenCodeSessionCatalogue:
    return OpenCodeSessionCatalogue(project_paths, OpenCodeCliRunner())


def descriptor() -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    `hooks` names the plugin install this provider accepts, added 2026-09-06. OpenCode publishes
    no hook-command mechanism, which is why this capability was a declared `None` for as long as
    it was; what it does publish is a plugin API, and `install-agent-hooks --provider opencode`
    writes one entry naming a file this project generates. The capability answers "can this
    provider be wired to report activity", and it can.

    `remote_control` stays a declared `None`: OpenCode has no host-level toggle (DEC-061 --
    absence is declared, never invented).
    """
    return ProviderDescriptor(
        ProfileId("opencode"),
        # A triangle, and the shape is the whole reason. Every *status* mark this project
        # draws is a circle, so no provider mark may be one; codex and cursor took the two
        # diamonds and claude the star, which left the purple square sharing a primitive with
        # codex's diamond -- a square and a rotated square, in adjacent hues, which is the
        # pair that collapses first under red-green colour-vision deficiency. Changed on the
        # owner's instruction 2026-09-09 after the gate evaluator raised it. A triangle is the
        # one basic shape nothing else here uses, so the four now differ by shape alone and
        # colour is only the second signal (DEC-010).
        glyph="🔺",
        sessions=_sessions,
        usage=OpenCodeUsageReader(),
        hooks="opencode",
    )
