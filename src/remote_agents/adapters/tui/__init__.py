"""Local terminal driver adapter for the owner's own host."""

from __future__ import annotations

#: The console's three panes, by the name `remote-agents pane <name>` takes.
#:
#: Declared here, in a module that imports nothing, because the composition root needs the
#: *names* to build its argument parser while every module that knows what a pane *is*
#: imports Textual — and `serve` must never load the terminal library (a failure in it would
#: then reach the bot). `panes.PANE_SURFACES` is keyed off this tuple rather than repeating
#: it, so the parser's `choices` and the surfaces it routes to cannot drift apart.
PANE_NAMES: tuple[str, ...] = ("projects", "sessions", "feed")
