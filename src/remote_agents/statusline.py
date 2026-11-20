"""The status-line hop: record Claude's own limits reading, then run the owner's status line.

Claude Code hands its `statusLine` command a JSON document on stdin, and for a Pro/Max
subscriber that document carries `rate_limits` -- the five-hour and seven-day windows as the
plan itself counts them, which no file on this host otherwise holds. The installer wraps the
owner's existing command in this one: the hop copies `rate_limits` to
`<state>/claude-limits.json` with the time it was seen, then runs the previous command on the
same bytes and exits with its status, so the bar renders exactly as before.

It lives beside the composition root for the same reason `agent_event` does, only more so:
Claude Code runs the status-line command on every update (debounced at 300 ms) in every
session on the machine, and `bootstrap` costs ~678 modules and a quarter second before it
could learn it has nothing to do. So module scope is stdlib only, every package import is
deferred to the branch that needs it, `__main__` routes here before `bootstrap` is imported,
and `bootstrap` delegates to `hop_from_stdin` so there is one implementation.

DEC-013 says what a global hook carries is rendered, never stored. This hop *stores* a
reading, and that is allowed because the reading is a host fact -- the plan's windows,
reported identically to every session on the account -- and not a session's words.

It keeps one more thing, for a managed session only (DEC-089 as amended by Sub-plan 5): when
its environment carries a well-formed `REMOTE_AGENTS_SESSION_ID` and the document a numeric
`context_window.used_percentage`, it writes `{"used_percentage", "context_window_size",
"recorded_at"}` to `<state>/claude-context/<session id>.json`. That figure is Claude's own
count of the context in use, and it exists nowhere else on the host: the transcript reader
can only estimate it (BL-118). Nothing else from the document is kept: not the model, not
the working directory, not the transcript path, not the cost. And nothing here may break the
bar: malformed JSON, no `rate_limits`, no state directory, an unwritable one, no stdin at all
-- each skips the write and still runs the owner's command.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

#: The reading's name under the service's state directory.
LIMITS_FILE_NAME = "claude-limits.json"

#: The directory, under the service's state directory, holding one context record per
#: managed session. Named here rather than imported for `LIMITS_FILE_NAME`'s reason; the two
#: spellings are pinned equal by
#: `test_claude_context_directory_is_under_the_state_directory_and_named_as_the_hop_writes_it`.
CONTEXT_DIRECTORY_NAME = "claude-context"

#: The largest context window a record may name. Today's largest is a million tokens; a
#: hundred million leaves room for growth and keeps a pathological integer off the disk.
_MAX_WINDOW_TOKENS = 100_000_000

#: How old an abandoned temporary must be before a later hop collects it. Claude Code cancels
#: an in-flight hop when a newer update arrives, so a temporary between `os.open` and
#: `os.replace` is a normal casualty, and nothing else owns this directory's litter. An hour
#: is far past any write that is still in progress and short enough that the directory
#: never holds more than a handful.
_ABANDONED_AFTER_SECONDS = 3600.0


def _rate_limits(payload: bytes) -> object | None:
    """The `rate_limits` member of the document, or `None` when there is nothing to record."""
    try:
        document = json.loads(payload)
    except ValueError:
        # `JSONDecodeError` and the `UnicodeDecodeError` of non-UTF-8 bytes are both this.
        return None
    if not isinstance(document, dict) or "rate_limits" not in document:
        return None
    return document["rate_limits"]


def _context_window(payload: bytes) -> tuple[float, int] | None:
    """`(used_percentage, context_window_size)`, or `None` when there is no figure to keep.

    The percentage must be a real number from 0 to 100: Claude Code sends null before the
    first turn (seen 2026-11-20), and a bool is an `int` to Python but no percentage. The size
    must be a positive integer no larger than `_MAX_WINDOW_TOKENS`: the reader shows the
    percentage against it, and a record without a usable one is not written at all, since the
    reader would discard it and fall back to the transcript estimate anyway.
    """
    try:
        document = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    window = document.get("context_window")
    if not isinstance(window, dict):
        return None
    figure = window.get("used_percentage")
    if isinstance(figure, bool) or not isinstance(figure, (int, float)):
        return None
    if not math.isfinite(figure) or not 0 <= figure <= 100:
        return None
    size = window.get("context_window_size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= _MAX_WINDOW_TOKENS:
        return None
    return float(figure), size


def _default_state_directory() -> Path | None:
    """The service's state directory, imported only on the path that writes."""
    from remote_agents.config import ConfigError
    from remote_agents.production import ProductionPaths

    try:
        return ProductionPaths.for_home(Path.home()).state_directory
    except (ConfigError, RuntimeError, OSError):
        return None


def record_limits(payload: bytes, state_directory: Path | None) -> None:
    """Write `{"rate_limits": <as received>, "recorded_at": <now>}` atomically at 0600.

    A missing directory is a reason not to write, not a reason to create it: the service
    owns that directory and its mode. The temporary name is a sibling so `os.replace` is a
    rename within one filesystem, and it is removed if anything after its creation fails.
    """
    rate_limits = _rate_limits(payload)
    if rate_limits is None:
        return
    resolved = state_directory if state_directory is not None else _default_state_directory()
    if resolved is None:
        return
    _collect_abandoned_temporaries(resolved)
    record = json.dumps({"rate_limits": rate_limits, "recorded_at": time.time()})
    _write_atomically(resolved, LIMITS_FILE_NAME, record)


def record_context(
    payload: bytes, state_directory: Path | None, environment: Mapping[str, str]
) -> None:
    """Write a managed session's context figure to `<state>/claude-context/<id>.json`.

    Only for a session id `ports.session_identity.safe_session_id` accepts -- it becomes a
    file name -- and only for a numeric percentage. The state directory must already exist,
    as for the limits file; the subdirectory is made here, at 0700, because no other writer
    exists for it (one that already exists keeps its mode). One that is a link, or not a
    directory, when it is checked is not written to; the state directory is the owner's
    alone, so only the owner could swap it between that check and the write.
    """
    # The id first: most sessions on the host are not managed, and they need no parse.
    # Deferred, like every package import here; `ports.session_identity` is stdlib-only.
    from remote_agents.ports.session_identity import SESSION_ID_VARIABLE, safe_session_id

    session = safe_session_id(environment.get(SESSION_ID_VARIABLE))
    if session is None:
        return
    figure = _context_window(payload)
    if figure is None:
        return
    resolved = state_directory if state_directory is not None else _default_state_directory()
    if resolved is None or not resolved.is_dir():
        return
    directory = resolved / CONTEXT_DIRECTORY_NAME
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return
    if directory.is_symlink() or not directory.is_dir():
        return
    name = f"{session}.json"
    _collect_abandoned_temporaries(directory, name)
    used_percentage, context_window_size = figure
    record = json.dumps(
        {
            "used_percentage": used_percentage,
            "context_window_size": context_window_size,
            "recorded_at": time.time(),
        }
    )
    _write_atomically(directory, name, record)


def _write_atomically(directory: Path, name: str, record: str) -> None:
    """`record` to `directory/name` at 0600 through a sibling temporary; failures are quiet."""
    # A random suffix, never the pid: a temporary left by a killed hop plus a reused pid made
    # `O_EXCL` refuse the next write under that pid, silently, for as long as the litter stood.
    temporary = directory / f".{name}.{os.urandom(6).hex()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(record.encode("utf-8"))
        os.replace(temporary, directory / name)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _collect_abandoned_temporaries(directory: Path, name: str = LIMITS_FILE_NAME) -> None:
    """Unlink this hop's own temporaries older than the bound; every failure is ignored."""
    cutoff = time.time() - _ABANDONED_AFTER_SECONDS
    try:
        for entry in directory.glob(f".{name}.*.tmp"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        return


def hop_from_stdin(then: str | None, state_directory: Path | None) -> int:
    """Record what stdin carried, then run `then` on the same bytes and return its status.

    Reads stdin whole before anything else: the previous command must see every byte Claude
    Code sent, and it is the same bytes the record is parsed from.
    """
    try:
        payload = sys.stdin.buffer.read()
    except (AttributeError, ValueError, OSError):
        # `sys.stdin` is None when the process was started with no stdin at all, a closed one
        # raises, and a read can fail. None of them is a reason to skip the owner's command.
        payload = b""
    try:
        record_limits(payload, state_directory)
    except Exception:  # noqa: BLE001 -- every failure, not every failure this module foresaw
        pass
    try:
        record_context(payload, state_directory, os.environ)
    except Exception:  # noqa: BLE001 -- the same contract, held separately for each record
        pass
    if then is None:
        return 0
    try:
        completed = subprocess.run(["sh", "-c", then], input=payload, check=False)
    except OSError:
        return 0
    # A signal death comes back negative; `sh` reports it as 128+N, and so must this hop,
    # since `SystemExit(-15)` becomes 241 rather than the 143 the owner's chain would have
    # returned on its own.
    code = completed.returncode
    return 128 - code if code < 0 else code


def run_statusline(argv: list[str] | None = None) -> int:
    """Parse this subcommand's own arguments, so reaching it needs no other parser."""
    # `NonEchoingArgumentParser`, like every other parser in this project, and for the reason
    # `agent_event` records: `__main__` routes here without `bootstrap`, so a parser that
    # echoed the operator's argv would do so on the shipped path. Deferred into the call
    # because it is a package import, even though `ports.argv_text` is itself stdlib-only.
    from remote_agents.ports.argv_text import NonEchoingArgumentParser

    parser = NonEchoingArgumentParser(prog="remote-agents statusline")
    parser.add_argument("--then", default=None)
    parser.add_argument("--state-dir", type=Path)
    arguments = parser.parse_args(argv)
    return hop_from_stdin(arguments.then, arguments.state_dir)
