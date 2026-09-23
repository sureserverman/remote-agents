"""Classify a pane capture as an idle composer, a running turn, a dialog, or none of those.

The prompt relay (DEC-099) types into a pane only when this answers IDLE, so the classifier is
built to fail towards *not* idle: a screen it cannot place is UNKNOWN, a dialog anywhere on the
screen wins over everything, and the composer is found by its structure at the bottom of the
capture rather than by a phrase that an agent's own output could also contain. The patterns are
the verticals' (`ProviderDescriptor.composer`); nothing here names a provider.
"""

from __future__ import annotations

import re
from enum import Enum

from remote_agents.adapters.tmux.trust import classify_trust_capture
from remote_agents.domain.trust import TrustState
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor


class PaneState(Enum):
    """What a capture shows, as far as typing a prompt into it is concerned."""

    IDLE = "idle"
    """The agent's composer is on screen and empty, with no turn running and no dialog up."""
    BUSY = "busy"
    """A turn is running."""
    COMPOSING = "composing"
    """The composer already holds text; a paste would be joined to it."""
    DIALOG = "dialog"
    """A dialog is up. Nothing is ever typed into one."""
    UNKNOWN = "unknown"
    """The screen is not recognisably this agent's composer. Never treated as idle."""


def _normalised(capture: str) -> str:
    """The capture without trailing spaces or blank lines, so `\\Z` is the last drawn row.

    Blank rows carry no meaning to any of the patterns -- agents space their layout differently
    at different sizes (Codex draws one between its composer and model line) -- and a draft is
    compared without its blank lines anyway.
    """
    return "\n".join(line.rstrip() for line in capture.splitlines() if line.strip())


def _found(patterns: tuple[str, ...], screen: str) -> bool:
    return any(re.search(pattern, screen, re.MULTILINE) for pattern in patterns)


def _draft(screen: str, declared: ComposerScreen) -> str | None:
    match = re.search(declared.composer, screen, re.MULTILINE)
    if match is None:
        return None
    lines = [
        re.sub(declared.draft_line, "", line).strip() for line in match.group("draft").splitlines()
    ]
    draft = "\n".join(line for line in lines if line)
    if any(re.fullmatch(pattern, draft) for pattern in declared.placeholders):
        return ""
    return draft


def composer_draft(capture: str, descriptor: ProviderDescriptor) -> str | None:
    """What the composer holds, line for line -- `""` when empty -- or None when none is found.

    Lines are compared stripped and without blank lines, which is how both sides of a
    comparison (the text pasted and the text read back) are normalised by the caller.
    """
    declared = descriptor.composer
    if declared is None:
        return None
    return _draft(_normalised(capture), declared)


def classify(capture: str, descriptor: ProviderDescriptor) -> PaneState:
    """IDLE only for an empty composer with no dialog and no running turn; see `PaneState`."""
    declared = descriptor.composer
    screen = _normalised(capture)
    if declared is None or not screen.strip():
        return PaneState.UNKNOWN
    if _found(declared.dialogs, screen):
        return PaneState.DIALOG
    draft = _draft(screen, declared)
    if draft is None:
        # A live trust dialog replaces the composer; a leftover one (cursor-agent keeps the box
        # drawn above its composer after it is answered) sits over a composer that is found.
        trust = descriptor.trust_dialog
        if trust is not None and classify_trust_capture(screen, trust) is TrustState.AWAITING:
            return PaneState.DIALOG
        return PaneState.UNKNOWN
    if _found(declared.busy, screen):
        return PaneState.BUSY
    return PaneState.COMPOSING if draft else PaneState.IDLE
