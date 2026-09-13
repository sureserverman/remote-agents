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
reported identically to every session on the account -- and not a session's words. Nothing
else from the document is kept: not the session id, not the model, not the working
directory. And nothing here may break the bar: malformed JSON, no `rate_limits`, no state
directory, an unwritable one, no stdin at all -- each skips the write and still runs the
owner's command.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

#: The reading's name under the service's state directory.
LIMITS_FILE_NAME = "claude-limits.json"


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
    record = json.dumps({"rate_limits": rate_limits, "recorded_at": time.time()})
    temporary = resolved / f".{LIMITS_FILE_NAME}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(record.encode("utf-8"))
        os.replace(temporary, resolved / LIMITS_FILE_NAME)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


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
    record_limits(payload, state_directory)
    if then is None:
        return 0
    try:
        return subprocess.run(["sh", "-c", then], input=payload, check=False).returncode
    except OSError:
        return 0


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
