"""Spool what an agent hook observed into the service's private activity directory.

This runs as a hook inside the agent's own process, not inside the service, which fixes two
things about it. The first is that it must never fail loudly: anything raised here surfaces in
the session the operator is working in, so losing one activity record is always the lesser
failure and every path below ends in exit status zero. The second is that it is reachable by
any agent on the machine, so the environment variable the service exports decides whether to
spool at all: absent or malformed, this writes nothing. That is a guarantee about a session
that merely *doesn't have* the variable - the ordinary case of an operator running claude by
hand - and it is worth stating exactly that narrowly. It is not a guarantee against a process
that sets the variable deliberately, and no check here could be: the spool is owner-only, the
hook runs as that owner, and anything else running as that owner can write into the directory
without going through this file at all.

What the guard below does buy, which the variable cannot, is that an *authorized* record
lands where it was meant to. Deciding who may spool says nothing about where the spool goes,
so the directory is opened through a check that refuses a symlink left lying in wait rather
than through a plain mkdir, which would follow one.

What lands here is deliberately narrower than what the hook receives. The notification the
service will send needs an event name, the field that discriminates that event, one short
line of detail, a session, and a time; the transcript path and working directory the payload
also carries would leak filesystem layout into a Telegram message, so they never leave here.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

MAXIMUM_PAYLOAD_BYTES = 32_768
MAXIMUM_DETAIL_CHARACTERS = 240

_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DISCRIMINATING_FIELDS = ("error_type", "notification_type", "end_reason")
_DETAIL_FIELDS = ("message", "last_assistant_message")
_MAXIMUM_NAME_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class ObservedAgentEvent:
    """The bounded shape of one hook observation, and the only shape that reaches disk."""

    session_id: str
    event: str
    reason: str | None
    detail: str | None
    observed_at: datetime

    def document(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "event": self.event,
            "reason": self.reason,
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat(),
        }


def spool_agent_event(
    payload: IO[bytes],
    *,
    activity_directory: Path,
    environment: Mapping[str, str] = os.environ,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Record one hook event privately, and always report success to the agent."""
    try:
        session_id = _safe_session_id(environment.get(SESSION_ID_VARIABLE))
        if session_id is None:
            return 0
        observed = _observed_event(payload, session_id, now())
        if observed is not None:
            _write_privately(observed, activity_directory)
    except Exception:
        # Catching broadly is correct exactly here and nowhere else in this package. This
        # frame is the boundary of a hook running inside the agent's process, so an escaping
        # exception would disrupt the session the operator is working in. Every unexpected
        # failure - an unreadable stream, a spool that is not a writable directory, a full
        # disk - costs one activity record and nothing more.
        return 0
    return 0


def _safe_session_id(value: str | None) -> str | None:
    """Accept only an opaque id, because this value alone becomes part of a filename.

    It arrives from the environment, so it is attacker-influenced text in the one place this
    package turns text into a path. Allowing nothing but an unpunctuated token makes a
    traversal, an absolute path, or an embedded NUL unrepresentable rather than filtered.
    """
    if value is None or not _SAFE_SESSION_ID.fullmatch(value):
        return None
    return value


def _observed_event(
    payload: IO[bytes], session_id: str, moment: datetime
) -> ObservedAgentEvent | None:
    """Read a bounded payload and keep only the fields a notification is built from."""
    raw = payload.read(MAXIMUM_PAYLOAD_BYTES + 1)
    if not raw or len(raw) > MAXIMUM_PAYLOAD_BYTES:
        return None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    event = _plain_token(document.get("hook_event_name"))
    if event is None:
        return None
    return ObservedAgentEvent(
        session_id=session_id,
        event=event,
        reason=_first(document, _DISCRIMINATING_FIELDS, _plain_token),
        detail=_first(document, _DETAIL_FIELDS, _detail_line),
        observed_at=moment.astimezone(UTC),
    )


def _first(
    document: Mapping[str, object], fields: tuple[str, ...], read: Callable[[object], str | None]
) -> str | None:
    """Return the first field of a group this event actually carries.

    The four hook events name their discriminating field differently, and only one of those
    names is ever present, so reading them as a group avoids branching on the event name and
    keeps an event added upstream from silently losing its detail line.
    """
    values = (read(document.get(field)) for field in fields)
    return next((value for value in values if value is not None), None)


def _plain_token(value: object) -> str | None:
    """Accept an enumerated hook value only in the unpunctuated form the documentation uses."""
    return value if isinstance(value, str) and _PLAIN_TOKEN.fullmatch(value) else None


def _detail_line(value: object) -> str | None:
    """Normalize free agent text to one bounded line, since a notification shows one line."""
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:MAXIMUM_DETAIL_CHARACTERS] if normalized else None


def _write_privately(observed: ObservedAgentEvent, activity_directory: Path) -> None:
    """Publish one owner-only file, named so the drain can order what it finds.

    The mode is given to ``os.open`` rather than repaired afterwards, so the file is never
    briefly readable by anyone else, and ``O_EXCL`` means a name already taken - by a second
    event within the same microsecond, or by anything else - is stepped over instead of
    overwritten. ``O_NOFOLLOW`` says the same thing about a link as the directory guard
    above says about the spool: this frame writes to the name it was given, or to nothing.
    """
    if open_private_directory(activity_directory) is None:
        return
    content = json.dumps(observed.document(), sort_keys=True).encode("utf-8")
    stamp = observed.observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    for attempt in range(_MAXIMUM_NAME_ATTEMPTS):
        suffix = "" if attempt == 0 else f"-{attempt}"
        path = activity_directory / f"{observed.session_id}-{stamp}{suffix}.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        return
