"""The local surface's two themes, and the variables every widget colours itself through.

Two `Theme` objects rather than CSS literals, because the redesign's one hard rule for colour is
that no widget names a hex value: every row, glyph and border reaches for a variable (`$success`,
`$text-muted`, `$selection`, …) and the theme decides what that resolves to. That is also what
keeps `NO_COLOR` honest -- the glyph and the word stay the signal, and colour is the second one
(DEC-010) -- because a variable that resolves to nothing changes no character on screen.

`relay-night` is the default. The choice is remembered by `adapters/tui/preferences.py`, beside
the project order and under the same total-read rule: an unreadable preference is a forgotten
choice, never a surface that will not start. Switching happens through Textual's own command
palette theme command; nothing here adds a key for it.

**Two variables are this project's own: `selection` and `text-dim`.** Textual's design system
derives `text-muted` and friends from the theme's colours but knows nothing of either of these,
so `RemoteAgentsTui.get_theme_variable_defaults` supplies a fallback for them -- which is what
lets the built-in themes (and the snapshot suite's pinned one) still resolve every rule this
surface writes.
"""

from __future__ import annotations

from textual.theme import Theme

RELAY_NIGHT = "relay-night"
RELAY_DAY = "relay-day"

#: The names the surface will remember; anything else the palette offers is used and forgotten.
RELAY_THEMES = (RELAY_NIGHT, RELAY_DAY)

DEFAULT_THEME = RELAY_NIGHT

THEMES: tuple[Theme, ...] = (
    Theme(
        name=RELAY_NIGHT,
        primary="#7FA7E8",
        secondary="#2A303C",
        accent="#A78BFA",
        success="#4FD18B",
        warning="#E3B341",
        error="#FF6B6B",
        background="#0F1115",
        surface="#161920",
        panel="#1E222B",
        foreground="#D6DAE3",
        dark=True,
        variables={
            "text-muted": "#7C8494",
            "text-dim": "#4F5665",
            "selection": "#263449",
            "border": "#2A303C",
            # The footer: key in the warning colour, description muted (the redesign's
            # footer spec), reached through Textual's own footer variables so the widget
            # itself is untouched.
            "footer-key-foreground": "#E3B341",
            "footer-description-foreground": "#7C8494",
            "footer-background": "#161920",
            "footer-item-background": "#161920",
            "footer-description-background": "#161920",
            "footer-key-background": "#161920",
        },
    ),
    Theme(
        name=RELAY_DAY,
        primary="#2F6FD1",
        secondary="#D9D9D2",
        accent="#7C3AED",
        success="#1E9E5A",
        warning="#B7791F",
        error="#D14343",
        background="#FAFAF7",
        surface="#FFFFFF",
        panel="#F1F1EC",
        foreground="#1C1F26",
        dark=False,
        variables={
            "text-muted": "#6B7280",
            "text-dim": "#A1A1AA",
            "selection": "#E4ECFA",
            "border": "#D9D9D2",
            "footer-key-foreground": "#B7791F",
            "footer-description-foreground": "#6B7280",
            "footer-background": "#FFFFFF",
            "footer-item-background": "#FFFFFF",
            "footer-description-background": "#FFFFFF",
            "footer-key-background": "#FFFFFF",
        },
    ),
)

#: What `$selection` and `$text-dim` resolve to under a theme that does not define them.
#: Neutral greys rather than either relay palette's values, so a foreign theme is not quietly
#: given one relay theme's accent.
VARIABLE_DEFAULTS: dict[str, str] = {
    "selection": "#3a3a3a 40%",
    "text-dim": "#808080",
}
