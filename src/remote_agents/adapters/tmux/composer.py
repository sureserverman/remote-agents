"""Classify a pane capture as an idle composer, a running turn, a dialog, or none of those.

The prompt relay (DEC-099) types into a pane only when this answers IDLE, so the classifier is
built to fail towards *not* idle: a screen it cannot place is UNKNOWN, a dialog anywhere on the
screen wins over everything, and the composer is found by its structure at the bottom of the
capture rather than by a phrase that an agent's own output could also contain. The patterns are
the verticals' (`ProviderDescriptor.composer`); nothing here names a provider.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum

from remote_agents.adapters.tmux.trust import classify_trust_capture
from remote_agents.domain.trust import TrustState
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor
from remote_agents.ports.terminal import PromptReason


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


REFUSAL_FOR: dict[PaneState, PromptReason] = {
    PaneState.BUSY: PromptReason.BUSY,
    PaneState.COMPOSING: PromptReason.COMPOSING,
    PaneState.DIALOG: PromptReason.DIALOG,
    PaneState.UNKNOWN: PromptReason.UNRECOGNISED,
}
"""The reason a pane that is not IDLE gives for refusing a message."""


def prompt_text(text: str) -> str:
    """The owner's message as it may be pasted: newlines kept, every other control removed.

    A bracketed paste is ended early by `ESC [201~`, and a CR or an ETX inside it would act as a
    key in an agent that stopped honouring the brackets, so none of them reaches the buffer.
    Line endings are folded to `\\n` first, so a CRLF from a phone keeps its line break.
    """
    folded = text.replace("\r\n", "\n").replace("\r", "\n")
    for separator in ("\u2028", "\u2029", "\u0085"):
        folded = folded.replace(separator, "\n")
    # Format characters too (Cf: BOM, zero-width space, bidi overrides): invisible, so they
    # could stand in front of a `!` or `/` and hide it from the checks that refuse one, while
    # an agent that trims them would still read the command.
    kept = "".join(
        character
        for character in folded
        if character == "\n" or unicodedata.category(character) not in ("Cc", "Cf")
    )
    return kept.strip()


def _same_text(draft: str, text: str) -> bool:
    """Whether a composer's draft is the pasted text, allowing for how the pane wrapped it."""
    # All whitespace removed, not collapsed: a long token (a URL, a path) wraps in the pane with
    # no space at the break, and a collapsed comparison would never match it.
    return "".join(draft.split()) == "".join(text.split())


def enter_refusal(capture: str, descriptor: ProviderDescriptor, text: str) -> PromptReason | None:
    """Why `Enter` may not be pressed on this capture of a pasted `text`, or None if it may.

    Pressed only when the composer shows exactly the draft (or the placeholder a long paste
    folds into) and no dialog: every measured approval dialog opens on its yes option, so an
    `Enter` on a dialog that arrived after the idle check would approve it (DEC-099). A message
    beginning with `/` needs one thing more -- a command menu whose first entry is the command
    typed, because `Enter` runs the menu's entry rather than the text.
    """
    declared = descriptor.composer
    if declared is None:
        return PromptReason.NO_COMPOSER
    state = classify(capture, descriptor)
    if state is PaneState.DIALOG:
        return PromptReason.DIALOG
    if state is not PaneState.COMPOSING:
        return PromptReason.DRAFT_NOT_SEEN
    screen = _normalised(capture)
    draft = _draft(screen, declared) or ""
    folded = any(re.fullmatch(pattern, draft) for pattern in declared.folded)
    if not (folded or _same_text(draft, text)):
        return PromptReason.DRAFT_NOT_SEEN
    if text.startswith("/"):
        if declared.command_menu is None:
            return PromptReason.MENU
        # Fails closed: a menu the pattern cannot read is not a menu that agreed with the text.
        menu = re.search(declared.command_menu, screen, re.MULTILINE)
        if menu is None or menu.group("first") != text.split()[0]:
            return PromptReason.MENU
    return None
