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

Two keys since the 2026-09-02 redesign: `project_order` and `theme`. One file, one reader, one
writer that keeps whatever key it is not changing.
"""

from __future__ import annotations

import contextlib
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

#: What the project-order row is called on the Settings screen.
#:
#: Here rather than in `application/`, and that is the one asymmetry between this row and the
#: three above it on that screen. Those name facts about the *machine* that both surfaces
#: render, so DEC-007 puts their wording where neither surface owns it. This names a choice
#: only the terminal can make -- the bot has one order by decision -- so `application/` would
#: be a home for a word no second renderer will ever read.
PROJECT_ORDER_TITLE = "Project order"

#: What each order is called, in the few words a row has for it.
#:
#: The projects pane drew these words first, as its title hint (`Projects · recent first · o
#: toggles order`). It now reads them from here rather than keeping its own table: two copies
#: with identical values are what DEC-043 calls a second renderer, and the pane and the row
#: would only have to disagree once for the owner to meet two names for one order.
PROJECT_ORDER_LABELS = {
    RECENCY: "recent first",
    ALPHABETICAL: "a–z",
}

_ORDER_KEY = "project_order"
_THEME_KEY = "theme"

#: The theme names this file will remember. Imported from `theme.py` would be the obvious
#: spelling; it is restated here so a preference module stays importable without Textual,
#: which `theme.py` needs -- and a test pins the two tuples equal.
THEMES = ("relay-night", "relay-day")
DEFAULT_THEME = "relay-night"

#: What the theme row is called, and what each theme is called on it.
#:
#: The row says *night* and *day* rather than the registered names, because `relay-` is this
#: project's namespace inside Textual's theme registry and not a word the owner chose. The
#: palette still offers the full names, and the two are not in competition: the palette is
#: where every theme Textual knows can be reached, this row is where the two the surface
#: *remembers* are.
THEME_TITLE = "Theme"
THEME_LABELS = {
    "relay-night": "night",
    "relay-day": "day",
}


def next_project_order(current: str) -> str:
    """Which order one press moves to, from whatever the file last said.

    Total, and answering an unknown value with the **default** rather than raising or keeping
    it -- the same answer `read_project_order` gives that value, so the one place an order
    this version does not know could have survived a press is closed. Two members, so a second
    press returns; the row needs no third state to reach every one it has.
    """
    index = PROJECT_ORDERS.index(current) if current in PROJECT_ORDERS else -1
    return PROJECT_ORDERS[(index + 1) % len(PROJECT_ORDERS)]


def next_theme(current: str) -> str:
    """Which theme one press moves to. Total in the way `next_project_order` is, and for the
    same reason: the palette may have left the app on a built-in this file will not store, and
    a press from there must land somewhere this file *will*."""
    index = THEMES.index(current) if current in THEMES else -1
    return THEMES[(index + 1) % len(THEMES)]


def _read_all(path: Path | None) -> dict[str, object]:
    """The whole preference file as a dict, or an empty one for every way that can fail."""
    if path is None:
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Absent is the ordinary case and unreadable is the surprising one, and neither is
        # worth a log line on a path that runs at every start.
        #
        # `UnicodeDecodeError` is a `ValueError` rather than an `OSError`, so `except OSError`
        # alone let a file of non-UTF-8 bytes escape -- and this reader is called from
        # `RemoteAgentsTui.__init__`, before Textual exists to catch anything, so it took down
        # every pane process on the host rather than one project list. That is the fifth
        # instance of a class this repo swept at a Stage 2 gate: `config.py` (twice),
        # `bootstrap._load_private_telegram_secrets` and `session_runner.load_intent` all
        # carry the same handler and the same note. Found by this stage's Tier-2 review,
        # reproduced against a file of raw bytes.
        return {}
    try:
        stored = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return stored if isinstance(stored, dict) else {}


def read_project_order(path: Path | None) -> str:
    """Return the remembered order, or the default for every way that can fail."""
    order = _read_all(path).get(_ORDER_KEY)
    # `in PROJECT_ORDERS` is the type check as well as the value check: a non-string cannot
    # be a member, so a future version's unknown order and a number both land on the default.
    return order if order in PROJECT_ORDERS else DEFAULT_PROJECT_ORDER


def read_theme(path: Path | None) -> str:
    """Return the remembered theme, or `relay-night` for every way that can fail.

    The same total read `read_project_order` makes, for the same reason: a preference is never
    a reason the surface will not start. An unknown name -- a theme a later version knew, or a
    built-in the owner picked once -- lands on the default rather than on an `InvalidThemeError`
    out of the constructor.
    """
    theme = _read_all(path).get(_THEME_KEY)
    return theme if theme in THEMES else DEFAULT_THEME


def write_project_order(path: Path | None, order: str) -> None:
    """Record the chosen order owner-only, and never raise for failing to."""
    if order not in PROJECT_ORDERS:
        # The reader forgives an unknown value; the writer must not be what creates one.
        _LOG.warning("refusing to store an unknown project order: %r", order)
        return
    _write_key(path, _ORDER_KEY, order)


def write_theme(path: Path | None, theme: str) -> None:
    """Record the chosen theme owner-only, and never raise for failing to.

    Only the two relay themes are stored. The palette offers Textual's built-ins too, and one
    of those is used for as long as the process lives and then forgotten -- a preference file
    naming a theme the reader does not accept would be a file the reader always ignores.
    """
    if theme not in THEMES:
        _LOG.debug("not storing a theme this surface does not remember: %r", theme)
        return
    _write_key(path, _THEME_KEY, theme)


def _write_key(path: Path | None, key: str, value: str) -> None:
    """Rewrite the file with one key changed and every other key kept.

    Read-modify-write rather than a bare dump, since the file gained a second key: writing the
    theme must not forget the order, and the reverse.
    """
    if path is None:
        return
    stored = _read_all(path)
    stored[key] = value
    payload = json.dumps(stored)
    # Written beside the target and renamed over it, rather than truncated in place. The
    # reader forgives a zero-length file, so an interrupted in-place write cost only a
    # forgotten preference -- but `os.replace` is atomic on the same filesystem and removes
    # the window entirely for two lines. A reader on another surface can then never see a
    # half-written file, only the old one or the new one.
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        # O_EXCL because this name is ours: a collision means another process is mid-write
        # with our pid, which cannot happen, or a stale file is in the way, which we want to
        # hear about rather than clobber. fchmod rather than chmod names the file just
        # opened, not whatever the path resolves to a syscall later.
        descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
        # Replaces whatever mode the old file had along with its contents, so this is also
        # what repairs one an earlier version -- or an operator -- left readable.
        os.replace(scratch, path)
    except OSError:
        _LOG.warning("could not save the preference %s to %s", key, path, exc_info=True)
        # A scratch file left behind is a file the next write cannot create, so the failure
        # would be permanent rather than transient.
        with contextlib.suppress(OSError):
            scratch.unlink()
