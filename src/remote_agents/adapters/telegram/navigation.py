"""Global destinations on the private bot's interactive screens."""

from collections.abc import Callable

from remote_agents.adapters.telegram.presenters import Button
from remote_agents.adapters.telegram.stops import CONFIRMED_FORCE
from remote_agents.application.session_actions import CLEANUP, FORCE, GRACEFUL

_DESTINATIONS = (
    ("sessions", "Sessions", "sessions.open", "sessions"),
    ("launch", "Launch", "launch.open", "projects"),
    ("resume", "Resume", "resume.open", "projects"),
    ("settings", "Settings", "settings.open", "default"),
)

_FLOW = {
    "sessions": "sessions",
    "session": "sessions",
    "remote": "sessions",
    "launch": "launch",
    "project": "launch",
    "resume": "resume",
    "sched": "launch",
    "schedule": "launch",
    "schedules": "sessions",
    "settings": "settings",
    "host": "settings",
}


def flow_of(action: str) -> str | None:
    if action in {GRACEFUL, CLEANUP, FORCE, CONFIRMED_FORCE}:
        return "sessions"
    return _FLOW.get(action.partition(".")[0])


def navigation_row(
    flow: str | None, *, can_resume: bool, callback: Callable[[str, str], str]
) -> tuple[Button, ...]:
    return tuple(
        Button(f"• {label}" if flow == tab else label, callback(action, entity))
        for tab, label, action, entity in _DESTINATIONS
        if tab != "resume" or can_resume
    )
