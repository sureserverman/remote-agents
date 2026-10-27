"""An optional explicit choice, shared by Settings and both start flows."""

from collections.abc import Awaitable, Callable

from remote_agents.adapters.tui.model import _BACK
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.application.permission_presets import (
    PERMISSION_CHOICES,
    PERMISSION_EFFECT,
    PERMISSION_INHERIT_LABEL,
    PERMISSION_TITLE,
)
from remote_agents.domain.profiles import PermissionPreset


class PermissionsScreen(ChoiceScreen):
    empty_state = NEVER_EMPTY
    position = "PERMISSIONS"
    crumb = PERMISSION_TITLE

    def __init__(
        self,
        current: PermissionPreset | None,
        save: Callable[[PermissionPreset | None], Awaitable[None]],
        *,
        allow_inherit: bool = False,
    ) -> None:
        super().__init__()
        self.current = current
        self.save = save
        self.allow_inherit = allow_inherit

    async def populate(self) -> None:
        self.hide_entry()
        self.set_status(
            "Choose for this session only; the Settings default stays unchanged."
            if self.allow_inherit
            else PERMISSION_EFFECT
        )
        entries = [
            (choice.value.value, f"{choice.label} · {choice.description}")
            for choice in PERMISSION_CHOICES
        ]
        if self.allow_inherit:
            entries.insert(0, ("inherit", PERMISSION_INHERIT_LABEL))
        selected = self.current.value if self.current is not None else "inherit"
        self.show_choices(
            tuple(entries),
            highlight=next(
                (i for i, (key, _) in enumerate(entries) if key == selected), len(entries)
            ),
            trailing=((_BACK, "Cancel"),),
        )

    async def choose(self, key: str) -> None:
        if key == _BACK:
            await self.tui.go_back()
            return
        try:
            value = None if key == "inherit" and self.allow_inherit else PermissionPreset(key)
        except ValueError:
            self.announce("That permission choice is no longer available.")
            return
        async with self.holding_the_guard():
            try:
                await self.save(value)
            except Exception as error:
                self.announce(f"Could not save launch permissions: {error}")
                return
        await self.tui.go_back()
