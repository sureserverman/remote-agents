"""Which managed session a hook event belongs to, when the hook does not run in the pane (DEC-126).

A Claude hook runs as a child of the agent in the managed pane, so the session variable the pane
was launched with names its session. A Codex hook does not: Codex runs every turn in one
app-server daemon per `CODEX_HOME`, the pane is only a client of it, and the daemon spawns the
hook. The daemon carries the environment of whichever pane started it -- measured on 2026-10-13,
when the `~/.codex` daemon an obsidian-wiki pane had started on 10-08 was still filing an
engineering-skills session's asks under the obsidian-wiki session four days after it ended.

So for such a provider the service writes a **roster** of its live managed sessions, and the
hook names its session from what the payload says instead: the provider's own thread id and the
folder the turn runs in. The rule, in order:

1. a thread id equal to a session's `resume_source_id` is that session's, folder or not -- a
   resumed conversation began before its session did, so only the exact id can claim it;
2. otherwise, among the sessions working in the payload's folder whose `created_at` is no later
   than the thread's own creation time, the latest -- a session's thread is minted at its launch,
   after its record, so of the sessions sharing a folder it is the newest one older than it;
3. otherwise, when exactly one session works in that folder, that one -- a thread the time rule
   cannot place (an older thread reopened inside the pane, an id with no time in it) still has
   only one session it can belong to;
4. otherwise nobody's, and the hook writes nothing.

The thread's creation time is read from the id itself: Codex mints version-7 UUIDs (RFC 9562),
whose first 48 bits are the Unix time in milliseconds. An id of any other shape has no time.

**What this cannot tell apart, and so gets wrong rather than silent.** Nothing in a payload ties
a thread to the pane that opened it, so the folder and the time are evidence, not proof:

- a Codex the owner starts by hand in a folder where a managed Codex session is live shares the
  daemon, and its threads are filed under that session -- where the environment rule filed
  them under whichever pane started the daemon, this files them somewhere plausible;
- with two managed sessions in one folder, a thread the older one opens after the newer one
  started (a `/new`, a reviewer or subagent thread) is filed under the newer one, and one the
  time rule cannot place is filed under neither;
- a subagent thread is filed under its parent's session, as the environment rule did.

Each is narrower than what it replaced: every Codex event on the host filed under whichever pane
happened to start the daemon (DEC-126 records the trade).

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.session_identity import safe_session_id

if TYPE_CHECKING:
    # Annotations only: the hook imports this module on every turn, and the port is the
    # service's side of it.
    from remote_agents.ports.hook_roster import RosterSession

#: Beside the turn markers' directory, never among the records: the drain reads only the spool's
#: top-level `*.json`, and a roster there would be read as a record.
ROSTER_DIRECTORY = "identity"

#: The roster's own bound. A host runs a handful of sessions; a file this size is not a roster.
_MAXIMUM_ROSTER_BYTES = 262_144

_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One live managed session (`notifiable`) a payload-identified hook may belong to."""

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
    if not isinstance(thread_id, str):
        return None
    entries = tuple(roster)
    for entry in entries:
        if entry.resume_source_id is not None and entry.resume_source_id == thread_id:
            return entry.session_id
    # Absolute only: `realpath` would resolve a relative folder against the hook's own.
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    try:
        workspace = os.path.realpath(cwd)
    except (OSError, ValueError):
        return None
    here = [entry for entry in entries if entry.workspace == workspace]
    minted = thread_created_at(thread_id)
    older = [entry for entry in here if minted is not None and entry.created_at <= minted]
    if older:
        return max(older, key=lambda entry: entry.created_at).session_id
    if len(here) == 1:
        return here[0].session_id
    return None


class _Untrusted:
    """What `inspect_roster` answers for a roster file that is there and cannot be trusted."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNTRUSTED"


UNTRUSTED = _Untrusted()


def read_roster(path: Path) -> tuple[RosterEntry, ...] | None:
    """The roster at `path`: None when there is none, `()` when it cannot be trusted.

    The two answers mean different things to the hook. None means the service never wrote one,
    so the hook keeps the environment rule it had. An empty tuple means the service has spoken
    and names nobody -- which is also the answer for a file that is not the service's: a
    symlink, a pipe or anything else that is not a regular file, a file others may read or
    write, one owned by someone else, or one that does not parse. Failing closed costs a
    notification; failing open is the incident this module exists to end.
    """
    found = inspect_roster(path)
    return () if found is UNTRUSTED else found


def inspect_roster(path: Path) -> tuple[RosterEntry, ...] | _Untrusted | None:
    """The roster at `path`, keeping apart what `read_roster` folds together.

    None when there is none; `UNTRUSTED` for a file that is there and cannot be trusted, on
    every ground `read_roster` names; otherwise its entries, `()` when the service listed no
    session. The hook needs only "names nobody" for the last two. `remote-agents session-id`
    needs them apart: beside an untrusted roster it cannot tell a daemon shell from any other,
    beside an empty one it can (DEC-128).
    """
    try:
        # Non-blocking, because opening a pipe for reading waits for a writer, and this runs in
        # every Codex turn; the regular-file check below then refuses it.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        return UNTRUSTED
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_mode & 0o077
            or status.st_uid != os.geteuid()
        ):
            return UNTRUSTED
        raw = os.read(descriptor, _MAXIMUM_ROSTER_BYTES + 1)
    except OSError:
        return UNTRUSTED
    finally:
        os.close(descriptor)
    if len(raw) > _MAXIMUM_ROSTER_BYTES:
        return UNTRUSTED
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        # Nesting past the parser's depth raises `RecursionError`, not `ValueError`.
        return UNTRUSTED
    sessions = document.get("sessions") if isinstance(document, dict) else None
    if not isinstance(sessions, list):
        return UNTRUSTED
    entries = []
    for item in sessions:
        entry = _entry(item)
        if entry is None:
            # One bad entry makes the whole file untrustworthy: a partial roster would let the
            # time rule hand a thread to the wrong session in the same folder.
            return UNTRUSTED
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
    """Replace the roster at `path` atomically, owner-only, creating its directory owner-only.

    Through `open_private_directory`, never a plain `mkdir(parents=True)`: that would make a
    missing spool directory with the umask's mode, and refuse nothing standing in for it as a
    link -- the spool directory this roster sits under is the one every hook writes into.
    """
    if open_private_directory(path.parent) is None:
        raise OSError(f"refusing to write a roster through a link or a shared directory: {path}")
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
    encoded = json.dumps(document).encode("utf-8")
    if len(encoded) > _MAXIMUM_ROSTER_BYTES:
        # Every hook would read it as naming nobody, silencing the provider; say so here instead.
        raise ValueError(f"a roster of {len(encoded)} bytes is past what a hook will read")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".roster-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
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


class FileHookRoster:
    """The service's side: write each hook's roster under the spool, folders made real (DEC-126)."""

    def __init__(self, activity_directory: Path) -> None:
        self._activity_directory = activity_directory

    def write(self, hook_name: str, sessions: Sequence[RosterSession]) -> None:
        """Replace `hook_name`'s roster with `sessions`, each folder as `realpath` answers it."""
        write_roster(
            roster_path(self._activity_directory, hook_name),
            (
                RosterEntry(
                    session.session_id,
                    os.path.realpath(session.workspace),
                    session.created_at,
                    session.resume_source_id,
                )
                for session in sessions
            ),
        )
