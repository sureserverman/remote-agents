"""Button labels and their visible marks."""

from __future__ import annotations

from remote_agents.application.session_actions import ACTION_LABELS, CLEANUP, FORCE, GRACEFUL

_ACTION_EMOJI: dict[str, str] = {
    GRACEFUL: "\u23f9",  # ⏹
    CLEANUP: "\U0001f9f9",  # 🧹
    FORCE: "\u26d4",  # ⛔
}
"""The mark in front of each stop button. `ACTION_LABELS` stays the shared word; the mark is
the bot's, because Telegram has no separator and shape plus a glyph is what tells a stop from a
read on a keyboard. `action_button_label` is the one composer, and `unmarked` its inverse."""

_REMOTE_EMOJI = "\U0001f4e1"  # 📡
_LIMITS_EMOJI = "\U0001f4ca"  # 📊

_INSPECT_EMOJI = "\U0001f4c4"  # 📄

_RENAME_EMOJI = "\u270f\ufe0f"  # ✏️

_ATTACH_EMOJI = "\U0001f4ce"  # 📎

_MESSAGE_EMOJI = "\u2709\ufe0f"  # ✉️

_ROLLOVER_EMOJI = "\U0001f501"  # 🔁


def action_button_label(action: str) -> str:
    """`⏹ Stop and close` -- the shared label behind this surface's mark."""
    return f"{_ACTION_EMOJI[action]} {ACTION_LABELS[action]}"


def unmarked(label: str) -> str:
    """Recover the shared label from a button this surface marked, for anything decoding one.

    The inverse of `action_button_label` and of the read-action marks, and safe on a label that
    carries none: a mark is exactly one leading token followed by a space, and no shared label
    begins with one.
    """
    head, separator, rest = label.partition(" ")
    marks = {
        *_ACTION_EMOJI.values(),
        _INSPECT_EMOJI,
        _RENAME_EMOJI,
        _ATTACH_EMOJI,
        _REMOTE_EMOJI,
        _LIMITS_EMOJI,
        _MESSAGE_EMOJI,
        _ROLLOVER_EMOJI,
    }
    return rest if separator and head in marks else label
