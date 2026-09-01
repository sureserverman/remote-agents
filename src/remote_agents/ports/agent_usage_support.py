"""Provider-neutral helpers every usage reader shares: files, JSON, instants, windows.

Extracted from `adapters/agents/usage.py` ahead of the provider split, so four verticals ask
one module instead of carrying four copies (DEC-043). Nothing here knows a provider: these
are filesystem sweeps that treat every failure as empty, JSON reads that answer `None`, and
the one piece of window arithmetic (`_window`'s lapsed-window rule) that is about clocks
rather than about any provider's file format. The names keep their underscores because they
moved, not changed — each is the same symbol its callers already held.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from remote_agents.ports.agent_usage import UsageWindow

_TAIL_BYTES = 512 * 1024
"""How much of a conversation log's end is read to find its most recent accounting record.

Transcripts grow without bound and the answer is always near the end, so the whole file is
never read. Half a megabyte clears many turns of ordinary records on every provider here; a
log whose last accounting record is further back than that answers `None`, which is the same
answer as a log that has none — correct in both cases, and cheap.
"""

_START_TOLERANCE = timedelta(seconds=90)
"""How long before a session's recorded start its provider conversation may claim to begin.

Not slack for the sake of it. The service stamps `created_at` when it writes the record and
the provider stamps its own file when the agent finishes booting, so the two are minutes apart
in the ordinary direction — but a resumed or fast-starting agent can beat the store's write by
a moment, and clocks inside the same host still disagree at the edges of a second. Tolerating
a small negative gap costs nothing (a conversation from an hour ago is nowhere near it) and
not tolerating it loses the match outright.
"""

_LAPSED_WINDOW_GRACE = timedelta(minutes=1)
"""How far past its stated reset a rate-limit window may sit before it is treated as lapsed.

Skew between the process that wrote the figure and this one, and nothing else. See `_window`
for why a lapsed window is dropped rather than shown.
"""

_INF = float("inf")


def _window(
    label: str | None, percent: object, resets_at: datetime | None, *, now: object = None
) -> UsageWindow | None:
    """Build one window, dropping any figure that describes a window which has already reset.

    **The lapsed-window rule is the one piece of freshness logic that is not about file age.**
    Both sources record a rate limit as of the moment they were written, and neither rewrites
    it afterwards: Codex stamps `rate_limits` onto the `token_count` event of a turn, so a
    session that has been idle for a week still carries the percentages from its last turn.
    Observed on this host — a RUNNING codex session launched 2026-08-12 whose newest
    `token_count` was written on the 17th, reporting `week 43%` against a `resets_at` ten days
    in the past.

    A context window read from that same record is still correct, because a conversation that
    has taken no turns has not grown. A *rate limit* is not: the window it counted against has
    since closed and reopened, so the percentage is not slightly old, it is about something
    that no longer exists. Rendering it read `week 43% (resets in 0m)` — a number that is
    wrong and a countdown that says so without meaning to.

    So a lapsed window is dropped rather than aged or annotated. A window with no stated reset
    is kept, because nothing here can tell whether it lapsed and refusing it would throw away
    the only figure some future provider offers. The grace is for clock skew between the
    process that wrote the file and this one, and nothing more.
    """
    if label is None or isinstance(percent, bool) or not isinstance(percent, int | float):
        return None
    if not 0 <= percent <= 100:
        return None
    if resets_at is not None and resets_at < _moment(now) - _LAPSED_WINDOW_GRACE:
        return None
    return UsageWindow(label, float(percent), resets_at)


def _newest(paths: Iterable[Path]) -> Path | None:
    """The most recently written of a set of files, with no floor on how old it may be.

    `_newest_started_after` is the session read's version and carries a floor, because a
    conversation older than the session cannot be that session's. An account read has no
    session to compare against — every rollout on disk carries the same account's windows —
    so the floor would have nothing to mean here.
    """
    best: tuple[float, Path] | None = None
    for path in paths:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if best is None or modified > best[0]:
            best = (modified, path)
    return None if best is None else best[1]


def _newest_started_after(paths: Iterable[Path], started_at: datetime) -> Path | None:
    """Pick the most recently written conversation that did not predate this session.

    Two filters, and both are load-bearing. The floor on modification time throws out every
    conversation the owner had in this directory before the session existed — without it, a
    freshly launched agent that has not written a turn yet would be attributed the context of
    last week's conversation, which is worse than showing nothing. Choosing the newest of what
    survives is what picks the live one when several qualify.
    """
    floor = (started_at.astimezone(UTC) - _START_TOLERANCE).timestamp()
    best: tuple[float, Path] | None = None
    for path in paths:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified < floor:
            continue
        if best is None or modified > best[0]:
            best = (modified, path)
    return None if best is None else best[1]


def _last_json_line(path: Path, matches) -> dict | None:
    """Scan a JSON-lines log backwards from its end for the newest record a predicate accepts.

    The tail is read rather than the file, and the first line of that tail is discarded because
    a byte offset almost never lands on a record boundary — keeping it would feed a truncated
    document to the parser on nearly every call. Everything after it is a whole line by
    construction, so a parse failure there is a genuinely malformed record and is skipped in
    silence, as this whole module skips everything it cannot read.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _TAIL_BYTES:
                handle.seek(size - _TAIL_BYTES)
            raw = handle.read()
    except OSError:
        return None
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]
    for line in reversed(lines):
        document = _loads(line)
        if isinstance(document, dict) and matches(document):
            return document
    return None


def _freshest_json(paths: Iterable[Path], now: object) -> tuple[object, timedelta | None]:
    """Read the most recently written of a set of small JSON files, with its age."""
    best: tuple[float, Path] | None = None
    for path in paths:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if best is None or modified > best[0]:
            best = (modified, path)
    if best is None:
        return None, None
    try:
        document = _loads(best[1].read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None, None
    age = _moment(now) - datetime.fromtimestamp(best[0], UTC)
    return document, max(age, timedelta(0))


def _safe_glob(directory: Path, pattern: str) -> tuple[Path, ...]:
    """List a directory this project does not own, treating every failure as empty."""
    try:
        if not directory.is_dir():
            return ()
        return tuple(directory.glob(pattern))
    except OSError:
        return ()


def _escaped_workspace(workspace: Path) -> str:
    return str(_resolved(workspace)).replace("/", "-")


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


def _loads(value: object) -> object:
    if not isinstance(value, str | bytes | bytearray):
        return None
    try:
        return json.loads(value)
    except (UnicodeDecodeError, ValueError):
        return None


def _finite(value: object) -> bool:
    """Whether a provider's number can survive `int()` at all.

    `json.loads("1e400")` is `inf` — strictly valid JSON needing no `Infinity` literal — and
    `int(inf)` raises `OverflowError`, which is an `ArithmeticError` and so passed straight
    through catch sets built around `ValueError`. Checked at each conversion rather than only
    widened at the boundary, because a reader that answers `None` for one unreadable field is
    this module's whole contract and an exception that merely gets caught further out still
    costs every other field in the same read.
    """
    return not isinstance(value, float) or (value == value and value not in (_INF, -_INF))


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not _finite(value):
        return None
    return int(value) if value > 0 else None


_ISO_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _instant(value: object) -> datetime | None:
    """Read a reset time in either shape the two sources use, and nothing else.

    Codex writes a Unix second count; the status-line cache writes an ISO-8601 string with a
    `Z`. Both are accepted, anything else is `None`, and a naive ISO value is read as UTC —
    which is what both producers mean, and the same interpretation `session_store` documents
    for its own offset-less rows.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and _ISO_INSTANT.match(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _moment(now: object) -> datetime:
    return now() if callable(now) else datetime.now(UTC)
