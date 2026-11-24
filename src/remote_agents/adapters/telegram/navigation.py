"""Global destinations on the private bot's interactive screens."""

from collections.abc import Callable
from html import escape

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


def notice_line(notice: str | None) -> str:
    """The lead line, or nothing at all — never an empty line where a notice would be.

    `None` has to render byte-identically to the screen before this parameter existed,
    or every test pinning the sessions list becomes a test of this function instead.
    """
    return "" if notice is None else f"{escape(notice)}\n"


def _button_rows(buttons: tuple[Button, ...], width: int = 2) -> tuple[tuple[Button, ...], ...]:
    return tuple(tuple(buttons[index : index + width]) for index in range(0, len(buttons), width))


def _page_number(value: str) -> int:
    """Read a page from a token this bot minted, falling back to the first page."""
    try:
        return int(value)
    except ValueError:
        return 1
