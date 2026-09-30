"""Cursor's provider vertical: sessions, usage, and its descriptor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from remote_agents.adapters.agents.cursor.limit_screen import LIMIT_SCREEN
from remote_agents.adapters.agents.cursor.limits_source import CursorLimitsSource
from remote_agents.adapters.agents.cursor.sessions import CursorSessionCatalogue
from remote_agents.adapters.agents.cursor.usage import CursorUsageReader
from remote_agents.adapters.agents.cursor.usage_api import CursorUsageApiReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor, TrustDialog


def _sessions(project_paths: Mapping[ProjectId, Path]) -> CursorSessionCatalogue:  # noqa: ARG001
    # Cursor's catalogue is workspace-blind; the parameter keeps the factory contract one
    # shape for every provider.
    return CursorSessionCatalogue()


def _usage(
    limits_switch: Callable[[], str] | None, home: Path | None
) -> CursorUsageReader | CursorLimitsSource:
    """The constant reader alone, or the switch in front of it and the usage API behind it.

    Without a switch -- the default reader set, a test double's composition -- the constant
    reader is the capability, as it was before the API reader existed. With one, the router
    decides per account read, and off is still the constant answer (DEC-061/087: opt-in).
    """
    constant = CursorUsageReader()
    if limits_switch is None:
        return constant
    return CursorLimitsSource(limits_switch, CursorUsageApiReader(home=home), constant)


def descriptor(
    *, limits_switch: Callable[[], str] | None = None, home: Path | None = None
) -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    Hooks and `remote_control` both stay a declared None. `usage` is deliberately NOT None:
    a session read answers "I publish nothing", which renders as "not reported by this
    agent"; a None here
    would render "no conversation matched yet" forever (DEC-061 — the two must never
    conflate; the fold regression test pins the consequence). The account read answers the
    same unless the owner's switch is wired and on (`_usage`).

    The two keyword arguments are the owner's switch for the account read and the home the
    Cursor CLI's login lives under, both handed down by the composition root (DEC-046).
    """
    return ProviderDescriptor(
        ProfileId("cursor-agent"),
        glyph="🔶",
        sessions=_sessions,
        usage=_usage(limits_switch, home),
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
        # Measured on 2026.09.18 (`docs/acceptance-2026-09-22-composer-states.md`, captures in
        # `tests/fixtures/panes/cursor/`). The composer is the last `  → ` line, continued by
        # four-space lines, with the status line (`Auto · allowlist · …`, which wraps in a narrow
        # pane) under it. Its placeholder stays drawn while a turn runs; busy is `ctrl+c to stop`
        # or the `Working`/`Running` spinner. **The busy hint disappears while an approval is up**
        # and the "Tell the agent what to do instead" box is drawn where the composer is, so both
        # are dialogs, checked first. The trust box stays drawn after it is answered; its live
        # form is told by the navigation hint.
        composer=ComposerScreen(
            composer=(
                r"^  → (?P<draft>[^\n]*(?:\n    [^\n]*)*?)"
                r"\n(?:  (?! )[^\n]*\n){0,3}  (?! )[^\n]*·[^\n]*\Z"
            ),
            placeholders=(r"Plan, search, build anything", r"Add a follow-up"),
            # Shell mode puts `!` where `→` was (`stop_sequence/cursor_shell_mode_*.txt`).
            shell=(
                r"^  ! [^\n]*(?:\n    [^\n]*)*?"
                r"\n(?:  (?! )[^\n]*\n){0,3}  (?! )[^\n]*·[^\n]*\Z"
            ),
            busy=(r"ctrl\+c to stop", r"^ \S+ (?:Working|Running)\b"),
            dialogs=(
                r"^ Run this command\?",
                r"Skip & tell the agent what to do instead",
                r"^  → Tell the agent what to do instead",
                r"Use arrow keys to navigate, Enter to select",
            ),
        ),
        # Measured 2026-09-28 (`docs/acceptance-2026-09-28-limit-screens.md`); read off the pane
        # by the limit-screen watch, because this agent reports no limit event of its own.
        limit_screen=LIMIT_SCREEN,
    )
