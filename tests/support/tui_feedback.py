"""What the local surface said, read the same way from every test.

The surface has three places to say something and they mean different things — the header's
breadcrumb (where the owner is), the one-line status (what to do here), and a toast (what an
action just did). A test that reaches into whichever one it remembers is how the three drift
back into being one: an assertion on `#status` passes just as happily when a message that
belongs in a toast is rendered into the status line by mistake.

So these readers are the vocabulary: one per sink, plus `working` for the busy affordance,
which is a fourth thing the surface says and not a fourth place it says it.

`announcements` reaps expired notifications the way Textual's own renderer
does, which matters only in the direction that makes a test honest: a toast whose five
seconds elapsed during a slow test is gone from the surface too.
"""

from __future__ import annotations

from textual.widgets import OptionList


def status(app) -> str:
    """The one-line status of the position on screen."""
    return str(app.screen.query_one("#status").content)


def breadcrumb(app) -> str:
    """The trail in the header, which is what `Header` renders as the sub-title."""
    return app.screen.sub_title or ""


def announcements(app, *, severity: str | None = None) -> list[str]:
    """Every toast the surface has raised and not yet dropped, oldest first.

    `severity` filters to one kind, which is what a test asserting "this was reported as a
    failure" wants: the message text alone cannot tell an error from a confirmation, and the
    severity is half of what the split decided.
    """
    return [
        notification.message
        for notification in app._notifications
        if severity is None or notification.severity == severity
    ]


def working(app) -> bool:
    """Whether the position on screen is showing itself as busy with a command.

    The affordance is on `#choices` rather than on the screen — the rows are what must not be
    acted on while a command is in flight, and the status line underneath says what it is.
    """
    return bool(app.screen.query_one("#choices", OptionList).loading)
