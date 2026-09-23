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
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor


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

    `trust_dialog` stays a declared `None` too, and it is the one absence with a keypress behind
    it: OpenCode raises no folder-trust question on any host measured (fourteen seconds, twice,
    into a directory it had never been asked about -- `docs/acceptance-2026-09-09-trust-dialogs.md`
    section 3), so a Trust button on one of its sessions would send arrow keys and an Enter into
    a live prompt with no question on it. Said here rather than left to the field's default,
    because four other artifacts describe this vertical as *declaring* the absence and a reader
    who came looking found nothing to read (DEC-009).

    `reserved_keys` is the one non-empty reservation among the four providers, and it is a
    reading of this agent's own keybinds rather than a policy: OpenCode binds `F2` to
    `model_cycle_recent` (opencode.ai/docs/keybinds, read 2026-09-13; Shift+F2 cycles the other
    way and the leader is Ctrl+X). The F-key console binds its function keys as tmux *root*
    bindings, which take a key from every pane on the socket, so without this declaration an
    owner pressing F2 at an OpenCode pane would get the console instead of the model switch the
    agent binds -- and nothing would report it. Only plain `F2` belongs in the set: Shift+F2 is
    a different tmux key name and the console binds no shifted function key.
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
        # tmux's spelling, not Textual's: this set is handed to `tmux bind-key`/`send-keys`,
        # which knows `F2` and not `f2`.
        reserved_keys=frozenset({"F2"}),
        # Measured on 1.18.30 and 1.18.32 (`docs/acceptance-2026-09-22-composer-states.md`,
        # captures in `tests/fixtures/panes/opencode/`). The composer is the `┃` box closed by
        # `╹▀▀▀`, whose last inner line is the agent/model line (`Build · <model>`); its text lines
        # are the draft. The empty box stays drawn while a turn runs, so busy is the footer's
        # `esc interrupt`.
        composer=ComposerScreen(
            composer=(
                r"^(?P<draft>(?:[ \t]*┃[^\n]*\n)*?)[ \t]*┃[ \t]+\S+ · [^\n]*\n"
                r"[ \t]*╹▀+[^\n]*(?:\n[^\n]*){0,4}\Z"
            ),
            placeholders=(r'Ask anything… ".*"',),
            busy=(r"^[ \t]*\S+[ \t]+esc interrupt",),
            dialogs=(r"△ Permission required", r"Allow once +Allow always +Reject"),
            draft_line=r"^[ \t]*┃",
        ),
    )
