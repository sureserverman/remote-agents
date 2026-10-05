"""The handoff envelopes in `<project>/.claude/handoffs/`, read and written without trusting them.

The other end is coder-plugins' `handoff-envelope.py`, and the reading and writing rules here
are its rules, so that a file one side refuses the other refuses too. The directory is inside
a checkout: a test suite, a build script or an agent running there can put anything at any
name in it, and the daemon reads it unattended. So:

- Each component below the project -- `.claude`, `handoffs`, the file -- is opened with
  `O_NOFOLLOW` relative to its parent's descriptor. A link at any of them is refused, never
  followed, and a component swapped between the check and the open cannot redirect the read.
  The project directory itself is opened as given: it is configuration, not something a
  process in the checkout chose.
- A file is opened `O_NONBLOCK`, because a read-only open of a FIFO planted at an envelope's
  name otherwise waits for a writer, and the daemon waiting there stops every project's pass.
  It must then be a regular file of at most 4096 bytes, by `fstat` and by what a read returns.
- Only names of the form `<handoff id>.<ready|accepted|failed>.json` are opened. The writer's
  `.<id>.claim` files, its `.gitignore`, its temp files and `request.json` are never events.
- The body must be exactly the protocol's key set, version 1 as an integer (JSON `true` and
  `1.0` compare equal to 1 in Python, and neither is the version), an event matching the
  name's suffix, the name's own id, a session id by this service's own rule, and a UTC
  timestamp in the one format the writer produces.

A refused file is dropped and the rest are still read; nothing here raises, and a failure
reading one file never costs the others. A directory holding more than MAX_ENTRIES names is
refused whole, so planted names cannot make one pass unbounded. The `plan` path is carried as
an opaque string and never opened. It must be absolute, as the writer records it, and hold no
control, format or line-separator character (Unicode Cc, Cf, Zl, Zp): it is shown to the owner,
and a real path never holds one, while a bidi override or an escape would rewrite what they see.

`discard` removes a finished handoff's envelopes, so the directory does not fill over the
project's life; the writer's `.<id>.claim` stays, as its one-outcome record.

Whether `managed_session_id` names a session of this project is not checked here: that needs
the session store, and the rollover pass that holds it checks it.

Writes (`request.json`, and the `.gitignore` holding `*` that keeps all of it out of the
project's history) follow the writer's rules too: the directories are opened `O_NOFOLLOW |
O_DIRECTORY` relative to their parents, made 0700 if missing, and must be this user's; the
bytes go to a fresh `O_EXCL | O_NOFOLLOW` temp file beside the target and are renamed into
place, so a link planted at the target's name is replaced, never written through.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.ports.handoff_envelopes import FAILURE_CODES, HandoffEnvelope, HandoffEvent
from remote_agents.ports.session_identity import safe_session_id

PROTOCOL = "remote-agents-handoff"
VERSION = 1
CLAUDE_DIRECTORY = ".claude"
HANDOFF_DIRECTORY = "handoffs"
REQUEST = "request.json"
GITIGNORE = ".gitignore"
GITIGNORE_BODY = b"*\n"
#: The writer's own cap; an envelope is a few hundred bytes, so anything near this is not one.
MAX_BYTES = 4096
#: More names than this in the directory and none are read. A finished rollover's envelopes
#: are discarded and leave one claim behind, so this is thousands of rollovers deep.
MAX_ENTRIES = 4096
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_HANDOFF_ID = re.compile(r"h-[0-9a-f]{20}")
_ENVELOPE_NAME = re.compile(r"(h-[0-9a-f]{20})\.(ready|accepted|failed)\.json")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_UNSHOWABLE = frozenset({"Cc", "Cf", "Zl", "Zp"})
_EVENT_BY_SUFFIX = {
    "ready": HandoffEvent.READY,
    "accepted": HandoffEvent.ACCEPTED,
    "failed": HandoffEvent.FAILED,
}
_KEYS = frozenset(
    {"protocol", "version", "event", "handoff_id", "managed_session_id", "timestamp", "plan"}
)
_FAILED_KEYS = _KEYS | {"failure_code"}

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC
_CHILD_DIRECTORY = _DIRECTORY | os.O_NOFOLLOW
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC


class FileHandoffEnvelopes:
    """`HandoffEnvelopes` over the files coder-plugins' writer leaves in a project."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock

    def events(self, project_dir: Path) -> tuple[HandoffEnvelope, ...]:
        found: list[tuple[datetime, str, HandoffEnvelope]] = []
        try:
            with _handoff_directory(project_dir, create=False) as directory:
                if directory is None:
                    return ()
                names = _names(directory)
                for name in names:
                    match = _ENVELOPE_NAME.fullmatch(name)
                    if match is None:
                        continue
                    try:
                        envelope = _parsed(_read(directory, name), match[1], match[2])
                    except Exception:
                        envelope = None
                    if envelope is not None:
                        found.append((envelope.timestamp, name, envelope))
        except Exception:
            return ()
        return tuple(envelope for _, _, envelope in sorted(found, key=lambda f: f[:2]))

    def write_request(self, project_dir: Path, managed_session_id: str) -> bool:
        session = safe_session_id(managed_session_id)
        if session is None:
            return False
        body = {
            "protocol": PROTOCOL,
            "version": VERSION,
            "managed_session_id": session,
            "requested_at": self._clock().astimezone(UTC).strftime(TIMESTAMP_FORMAT),
        }
        try:
            with _handoff_directory(project_dir, create=True) as directory:
                if directory is None:
                    return False
                if not _gitignore_is_ours(directory):
                    _replace(directory, GITIGNORE, GITIGNORE_BODY)
                _replace(directory, REQUEST, (json.dumps(body) + "\n").encode("utf-8"))
        except Exception:
            return False
        return True

    def requested(self, project_dir: Path) -> str | None:
        try:
            with _handoff_directory(project_dir, create=False) as directory:
                return None if directory is None else _requested_session(directory)
        except Exception:
            return None

    def discard(self, project_dir: Path, handoff_id: str) -> None:
        if not _HANDOFF_ID.fullmatch(handoff_id):
            return
        try:
            with _handoff_directory(project_dir, create=False) as directory:
                if directory is None:
                    return
                for suffix in _EVENT_BY_SUFFIX:
                    try:
                        # unlink removes the name itself, never what a link there points at.
                        os.unlink(f"{handoff_id}.{suffix}.json", dir_fd=directory)
                    except FileNotFoundError:
                        pass
        except Exception:
            pass

    def clear_request(self, project_dir: Path, managed_session_id: str) -> None:
        try:
            with _handoff_directory(project_dir, create=False) as directory:
                if directory is not None and _requested_session(directory) == managed_session_id:
                    os.unlink(REQUEST, dir_fd=directory)
        except Exception:
            pass


@contextmanager
def _handoff_directory(project_dir: Path, *, create: bool) -> Iterator[int | None]:
    """A descriptor for `<project>/.claude/handoffs`, reached without following a link below
    the project; None when it is missing and `create` is false. Raises on a refusal."""
    descriptors: list[int] = []
    try:
        try:
            descriptors.append(os.open(project_dir, _DIRECTORY))
            for name in (CLAUDE_DIRECTORY, HANDOFF_DIRECTORY):
                descriptors.append(_child_directory(descriptors[-1], name, create=create))
        except FileNotFoundError:
            if create:
                raise
            yield None
            return
        yield descriptors[-1]
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _names(directory: int) -> list[str]:
    """The directory's names, sorted; raises past MAX_ENTRIES without listing the rest."""
    names: list[str] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(names) == MAX_ENTRIES:
                raise OverflowError("too many names in the handoff directory")
            names.append(entry.name)
    return sorted(names)


def _requested_session(directory: int) -> str | None:
    """The session `request.json` names, when it is a well-formed request; else None."""
    raw = _read(directory, REQUEST)
    if raw is None:
        return None
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("protocol") != PROTOCOL:
        return None
    return safe_session_id(body.get("managed_session_id"))


def _child_directory(parent: int, name: str, *, create: bool) -> int:
    try:
        descriptor = os.open(name, _CHILD_DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        descriptor = os.open(name, _CHILD_DIRECTORY, dir_fd=parent)
    if create and os.fstat(descriptor).st_uid != os.getuid():
        # Only a write insists on ownership, as the plugin's writer does: writing into a
        # directory someone else owns hands them the file.
        os.close(descriptor)
        raise PermissionError(f"{name} is not owned by this user")
    return descriptor


def _read(directory: int, name: str) -> bytes | None:
    """The file's bytes when it is a regular file of at most MAX_BYTES, else None."""
    try:
        descriptor = os.open(name, _READ, dir_fd=directory)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            return None
        # One byte past the cap, so a file that grew after the fstat is still refused.
        raw = os.read(descriptor, MAX_BYTES + 1)
        return raw if len(raw) <= MAX_BYTES else None
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    body = dict(pairs)
    if len(body) != len(pairs):
        # Two values for one key: which one a reader sees is a parser's choice, so neither.
        raise ValueError("duplicate key")
    return body


def _parsed(raw: bytes | None, name_id: str, suffix: str) -> HandoffEnvelope | None:
    """The envelope `raw` holds, if it passes every rule for the name it was found under."""
    if raw is None:
        return None
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except Exception:
        # Includes RecursionError from a deeply nested body: still only "not an envelope".
        return None
    event = _EVENT_BY_SUFFIX[suffix]
    if not isinstance(body, dict):
        return None
    if set(body) != (_FAILED_KEYS if event is HandoffEvent.FAILED else _KEYS):
        return None
    if body["protocol"] != PROTOCOL or type(body["version"]) is not int:
        return None
    if body["version"] != VERSION or body["event"] != event.value:
        return None
    handoff_id = body["handoff_id"]
    if not isinstance(handoff_id, str) or not _HANDOFF_ID.fullmatch(handoff_id):
        return None
    if handoff_id != name_id:
        return None
    session = safe_session_id(body["managed_session_id"])
    timestamp = _timestamp(body["timestamp"])
    if session is None or timestamp is None:
        return None
    plan = body["plan"]
    if not (isinstance(plan, str) or (plan is None and event is HandoffEvent.FAILED)):
        return None
    if isinstance(plan, str) and not _showable_path(plan):
        return None
    failure_code = body.get("failure_code")
    # A string first: `in` a frozenset hashes its operand, and a JSON list does not hash.
    if event is HandoffEvent.FAILED and not (
        isinstance(failure_code, str) and failure_code in FAILURE_CODES
    ):
        return None
    return HandoffEnvelope(
        event=event,
        handoff_id=handoff_id,
        managed_session_id=session,
        timestamp=timestamp,
        plan=plan,
        failure_code=failure_code,
    )


def _showable_path(plan: str) -> bool:
    return plan.startswith("/") and not any(
        unicodedata.category(character) in _UNSHOWABLE for character in plan
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _gitignore_is_ours(directory: int) -> bool:
    raw = _read(directory, GITIGNORE)
    return raw == GITIGNORE_BODY


def _replace(directory: int, name: str, body: bytes) -> None:
    """Put `body` at `name` by a fresh temp file renamed over it; raises, temp removed, on any
    failure. The temp name is random and created `O_EXCL`, so nothing planted can be it."""
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, _CREATE, 0o600, dir_fd=directory)
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(descriptor, view) :]
        finally:
            os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except OSError:
            pass
        raise
