"""The local surface's own preference file: read and written totally, never fatally.

This is the first thing this surface ever writes to disk, and the rule it is built on is
that a UI preference may never be a reason the surface will not start. Every read failure
-- absent, empty, unreadable, malformed, or carrying a value written by a version that knew
more orders than this one -- answers with the default, and every write failure is one log
line. The cost of any of them is that the owner's choice is forgotten, which is strictly
better than a terminal that refuses to draw a project list.

It lives in `adapters/tui` rather than `application` because it is this surface's alone:
the bot has one order by decision, and there is no shared rule here to promote (DEC-043
governs the *ordering*, which `application/project_catalog.py` owns; this file only
remembers which of the two the owner picked).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)

#: Ordered by recency of use, the way the bot's list has always opened.
RECENCY = "recency"
#: Ordered by area then name, for the owner who is looking a project up rather than
#: returning to the one they were just in.
ALPHABETICAL = "alphabetical"

PROJECT_ORDERS = (RECENCY, ALPHABETICAL)

#: Recency, because that is what the bot opens in and the two surfaces agree on the default
#: (DEC-012 -- what Stage 5 supersedes is that no picker knows a ranking exists, not which
#: order is the default).
DEFAULT_PROJECT_ORDER = RECENCY

_KEY = "project_order"


def read_project_order(path: Path | None) -> str:
    """Return the remembered order, or the default for every way that can fail."""
    if path is None:
        return DEFAULT_PROJECT_ORDER
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # Absent is the ordinary case and unreadable is the surprising one, and neither is
        # worth a log line on a path that runs at every start.
        return DEFAULT_PROJECT_ORDER
    try:
        stored = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return DEFAULT_PROJECT_ORDER
    if not isinstance(stored, dict):
        return DEFAULT_PROJECT_ORDER
    order = stored.get(_KEY)
    # `in PROJECT_ORDERS` is the type check as well as the value check: a non-string cannot
    # be a member, so a future version's unknown order and a number both land on the default.
    return order if order in PROJECT_ORDERS else DEFAULT_PROJECT_ORDER


def write_project_order(path: Path | None, order: str) -> None:
    """Record the chosen order owner-only, and never raise for failing to."""
    if path is None:
        return
    if order not in PROJECT_ORDERS:
        # The reader forgives an unknown value; the writer must not be what creates one.
        _LOG.warning("refusing to store an unknown project order: %r", order)
        return
    payload = json.dumps({_KEY: order})
    try:
        # O_CREAT's mode applies only to a file this call creates, so the fchmod is what
        # repairs one an earlier version -- or an operator -- left readable. fchmod rather
        # than chmod: it names the file just opened, not whatever the path resolves to a
        # syscall later.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
    except OSError:
        _LOG.warning("could not save the project order to %s", path, exc_info=True)
