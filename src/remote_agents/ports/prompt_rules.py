"""What may be typed into an agent's composer, decided before anything is pasted (DEC-099).

Asked by the terminal's guarded send at send time and by a schedule's validation at save time
(DEC-043), so a prompt the send would refuse is refused when the owner saves it rather than
hours later in an empty pane. A port and not `application/` because the tmux adapter asks it,
and an adapter may import only `domain` and `ports` (ARCH-02).
"""

from __future__ import annotations

import unicodedata

from remote_agents.ports.provider_descriptor import ComposerScreen
from remote_agents.ports.terminal import PromptReason


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


def text_refusal(text: str) -> PromptReason | None:
    """The refusals the text alone decides: nothing to type, or a shell command."""
    cleaned = prompt_text(text)
    if not cleaned:
        return PromptReason.EMPTY
    if cleaned.startswith("!"):
        return PromptReason.SHELL
    return None


def pre_paste_refusal(text: str, composer: ComposerScreen | None) -> PromptReason | None:
    """Why `text` may not be typed into an agent drawing `composer`, or None when it may.

    In the send's order: the text first, then whether the agent declares a composer at all,
    then a `/` command where the composer's command menu cannot be read -- pasted, `Enter`
    would run whatever the unreadable menu offers, and the stranded draft would refuse every
    later message.
    """
    refusal = text_refusal(text)
    if refusal is not None:
        return refusal
    if composer is None:
        return PromptReason.NO_COMPOSER
    if prompt_text(text).startswith("/") and composer.command_menu is None:
        return PromptReason.MENU
    return None
