"""Every position the local surface can be in, one `Screen` each.

`ALL_SCREENS` is the registry `tests/unit/adapters/tui/test_screen_back_paths.py`
parametrizes over, so a screen added without a working back path fails a test rather than
stranding the owner on it.

**Only navigable screens belong in this namespace.** The Stage 2 gate sweeps `vars()` here
for `Screen` subclasses missing from `ALL_SCREENS`, so re-exporting a base class — or
`Screen` itself — would fail a check that is asking a fair question. `ChoiceScreen` is
therefore imported from `.base` by whoever needs it, never from here.
"""

from __future__ import annotations

from remote_agents.adapters.tui.screens.launch import (
    LabelScreen,
    ProfilesScreen,
    ProjectsScreen,
    ReviewScreen,
)
from remote_agents.adapters.tui.screens.legacy import LegacyScreen

#: Every screen the owner can reach. Task 2.4 removes `LegacyScreen` from it along with the
#: step machine it hosts; until then it is registered like any other, because it is a
#: position the owner can be in and its back path is as worth proving as the rest.
ALL_SCREENS = (
    ProjectsScreen,
    ProfilesScreen,
    LabelScreen,
    ReviewScreen,
    LegacyScreen,
)

__all__ = [
    "ALL_SCREENS",
    "LabelScreen",
    "LegacyScreen",
    "ProfilesScreen",
    "ProjectsScreen",
    "ReviewScreen",
]
