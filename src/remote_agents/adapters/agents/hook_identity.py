"""Which managed session a hook event belongs to, when the hook does not run in the pane (DEC-126).

A Claude hook runs as a child of the agent in the managed pane, so the session variable the pane
was launched with names its session. A Codex hook does not: Codex runs every turn in one
app-server daemon per `CODEX_HOME`, the pane is only a client of it, and the daemon spawns the
hook. The daemon carries the environment of whichever pane started it -- measured on 2026-10-13,
when the `~/.codex` daemon an obsidian-wiki pane had started on 10-08 was still filing an
engineering-skills session's asks under the obsidian-wiki session five days after it ended.

So for such a provider the service writes a **roster** of its running managed sessions, and the
hook names its session from what the payload says instead: the provider's own thread id and the
folder the turn runs in. The rule, in order:

1. a thread id equal to a session's `resume_source_id` is that session's -- a resumed
   conversation began before its session did, so only the exact id can claim it;
2. otherwise, among the sessions working in the payload's folder whose `created_at` is no later
   than the thread's own creation time, the latest -- a session's thread is minted at its launch,
   after its record, and before any session launched later in the same folder;
3. otherwise nobody's, and the hook writes nothing.

The thread's creation time is read from the id itself: Codex mints version-7 UUIDs (RFC 9562),
whose first 48 bits are the Unix time in milliseconds. An id of any other shape has no time, and
only rule 1 can claim it.

Nothing here names a provider (DEC-122): which hooks get a roster is each vertical's licence's
answer, read by the service, and the hook only asks whether a roster for its own hook name
exists. Stdlib only, because the hook imports this on every turn of every session on the host.

The payload's folder is compared here and never written anywhere (DEC-013 clause 2).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.ports.session_identity import safe_session_id

#: Beside the turn markers' directory, never among the records: the drain reads only the spool's
#: top-level `*.json`, and a roster there would be read as a record.
ROSTER_DIRECTORY = "identity"

#: The roster's own bound. A host runs a handful of sessions; a file this size is not a roster.
_MAXIMUM_ROSTER_BYTES = 262_144

_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One running managed session a payload-identified hook may belong to."""

    session_id: str
    workspace: str
    """The folder the session's agent works in, as `os.path.realpath` answers it."""
    created_at: datetime
    resume_source_id: str | None = None
    """The provider conversation this session resumed, when it resumed one."""


def roster_path(activity_directory: Path, hook_name: str) -> Path:
    """Where the roster for one hook name lives under the spool."""
    return activity_directory / ROSTER_DIRECTORY / f"{hook_name}.json"


def thread_created_at(thread_id: object) -> datetime | None:
    """The creation time a version-7 UUID carries, or None for any other value."""
    if not isinstance(thread_id, str) or len(thread_id) != 36:
        return None
    if [len(part) for part in thread_id.split("-")] != [8, 4, 4, 4, 12]:
        return None
    digits = thread_id.replace("-", "").lower()
    if not set(digits) <= _HEX or digits[12] != "7" or digits[16] not in "89ab":
        return None
    return datetime.fromtimestamp(int(digits[:12], 16) / 1000, UTC)


def resolve(thread_id: object, cwd: object, roster: Iterable[RosterEntry]) -> str | None:
    """The session a payload belongs to under the module's rule, or None for nobody's."""
    if not isinstance(thread_id, str) or not isinstance(cwd, str) or not cwd:
        return None
    entries = tuple(roster)
    for entry in entries:
        if entry.resume_source_id is not None and entry.resume_source_id == thread_id:
            return entry.session_id
    minted = thread_created_at(thread_id)
    if minted is None:
        return None
    try:
        workspace = os.path.realpath(cwd)
    except (OSError, ValueError):
        return None
    candidates = [
        entry for entry in entries if entry.workspace == workspace and entry.created_at <= minted
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: entry.created_at).session_id


def read_roster(path: Path) -> tuple[RosterEntry, ...] | None:
    """The roster at `path`: None when there is none, `()` when it cannot be trusted.

    The two answers mean different things to the hook. None means the service never wrote one,
    so the hook keeps the environment rule it had. An empty tuple means the service has spoken
    and names nobody -- which is also the answer for a file that is not the service's: a
    symlink, a file others may read or write, one owned by someone else, or one that does not
    parse. Failing closed costs a notification; failing open is the incident this module exists
    to end.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return ()
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_mode & 0o077
            or status.st_uid != os.geteuid()
        ):
            return ()
        raw = os.read(descriptor, _MAXIMUM_ROSTER_BYTES + 1)
    except OSError:
        return ()
    finally:
        os.close(descriptor)
    if len(raw) > _MAXIMUM_ROSTER_BYTES:
        return ()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return ()
    sessions = document.get("sessions") if isinstance(document, dict) else None
    if not isinstance(sessions, list):
        return ()
    entries = []
    for item in sessions:
        entry = _entry(item)
        if entry is None:
            # One bad entry makes the whole file untrustworthy: a partial roster would let the
            # time rule hand a thread to the wrong session in the same folder.
            return ()
        entries.append(entry)
    return tuple(entries)


def _entry(item: object) -> RosterEntry | None:
    if not isinstance(item, dict):
        return None
    session_id = safe_session_id(item.get("session_id"))
    workspace = item.get("workspace")
    resumed = item.get("resume_source_id")
    created = item.get("created_at")
    if session_id is None or not isinstance(workspace, str) or not workspace:
        return None
    if resumed is not None and not isinstance(resumed, str):
        return None
    try:
        created_at = datetime.fromisoformat(created) if isinstance(created, str) else None
    except ValueError:
        return None
    if created_at is None or created_at.tzinfo is None:
        return None
    return RosterEntry(session_id, workspace, created_at, resumed)


def write_roster(path: Path, entries: Iterable[RosterEntry]) -> None:
    """Replace the roster at `path` atomically, owner-only, creating its directory owner-only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    document = {
        "sessions": [
            {
                "session_id": entry.session_id,
                "workspace": entry.workspace,
                "created_at": entry.created_at.isoformat(),
                "resume_source_id": entry.resume_source_id,
            }
            for entry in entries
        ]
    }
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".roster-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
