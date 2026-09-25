"""The console's status bar follows the theme the owner picks (sub-plan 2 Task 2.1).

tmux cannot read a Textual variable, so the bar's colours are resolved from the theme and
re-issued whenever the theme changes. Only a pane that is one of the console's own restyles
it: a stray `remote-agents tui` on the console's server is classified CONSOLE too.
"""

from __future__ import annotations

from backends import tui_context_for

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.theme import THEMES, status_bar_palette
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


class _Launcher:
    async def list_sessions(self):
        return ()


def _context(**overrides):
    arguments = {
        "sessions": _Launcher(),
        "projects": object(),
        "catalogue": (_EXISTING,),
        "profiles": (ProfileAvailability("claude", True),),
        "refresh_catalogue": lambda: (_EXISTING,),
        "attach_argv": lambda session_id: ("true",),
    }
    arguments.update(overrides)
    return tui_context_for(**arguments)


async def test_a_theme_switch_restyles_the_console_status_bar() -> None:
    issued = []

    async def restyle(palette) -> None:
        issued.append(palette)

    async def holds_slot() -> bool:
        return True

    app = RemoteAgentsTui(_context(console_status_bar=restyle, console_holds_slot=holds_slot))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.theme = "relay-day"
        await pilot.pause()
        await app.workers.wait_for_complete()

    night, day = (status_bar_palette(theme) for theme in THEMES)
    assert issued and issued[-1] == day
    assert night != day


async def test_a_process_outside_the_console_panes_leaves_the_bar_alone() -> None:
    issued = []

    async def restyle(palette) -> None:
        issued.append(palette)

    async def holds_slot() -> bool:
        return False

    app = RemoteAgentsTui(_context(console_status_bar=restyle, console_holds_slot=holds_slot))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.theme = "relay-day"
        await pilot.pause()
        await app.workers.wait_for_complete()

    assert issued == []


def test_a_translucent_panel_is_flattened_onto_the_window_not_swapped_for_night() -> None:
    from textual.theme import Theme

    theme = Theme(
        name="translucent-panel",
        primary="#ffffff",
        background="#000000",
        foreground="#eeeeee",
        warning="#ffcc00",
        success="#00ff00",
        error="#ff0000",
        # Textual refuses a strength on `panel=` itself; a variable is how one arrives.
        variables={"panel": "#ffffff 50%"},
    )

    palette = status_bar_palette(theme)

    assert palette.bar in {"#7F7F7F", "#808080"}
    assert palette.text == "#EEEEEE"
    assert palette != status_bar_palette(THEMES[0])
