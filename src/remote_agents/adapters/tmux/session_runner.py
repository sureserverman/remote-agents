"""Fixed executable replacing itself with one validated, persisted launch intent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.ports.argv_text import NonEchoingArgumentParser


@dataclass(frozen=True, slots=True)
class LaunchIntent:
    """Validated launch data loaded only by opaque session identifier."""

    session_id: SessionId
    profile_id: ProfileId
    executable: str
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]


def load_intent(session_id: SessionId, intent_directory: Path) -> LaunchIntent:
    """Reload and validate the exact intent bound to one managed session."""
    path = intent_directory / f"{session_id}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        # `UnicodeDecodeError` is a `ValueError` rather than an `OSError`, so a truncated or
        # corrupt intent file escaped as a decode traceback instead of the message this
        # function exists to give. Same class as the two handlers in `config.py`.
        raise ValueError("launch intent is unavailable or invalid") from error
    required = {"session_id", "profile_id", "executable", "argv", "cwd", "environment"}
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError("launch intent has an invalid schema")
    if SessionId.parse(document["session_id"]) != session_id:
        raise ValueError("launch intent does not match the managed session")
    executable = document["executable"]
    argv = document["argv"]
    cwd = Path(document["cwd"])
    environment = document["environment"]
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or not isinstance(argv, list)
        or not argv
        or any(not isinstance(argument, str) for argument in argv)
        or argv[0] != executable
        or not cwd.is_absolute()
        or not cwd.is_dir()
        or not isinstance(environment, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        )
    ):
        raise ValueError("launch intent contains unsafe process data")
    return LaunchIntent(
        session_id, ProfileId(document["profile_id"]), executable, tuple(argv), cwd, environment
    )


def run_session(session_id: SessionId, intent_directory: Path) -> NoReturn:
    """Replace this fixed runner with the validated profile executable and argv."""
    intent = load_intent(session_id, intent_directory)
    os.chdir(intent.cwd)
    os.execvpe(intent.executable, intent.argv, intent.environment)
    raise AssertionError("execvpe must not return")


def main() -> None:
    """Run a managed intent addressed only by a canonical session UUID."""
    # This one's argv is machine-built by the runtime rather than typed, so the leak is latent
    # here -- but "latent" is what the other one was until `__main__` routed to it. Every parser
    # in this project is the same class, and an architecture test says so.
    parser = NonEchoingArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--intent-dir", type=Path, required=True)
    arguments = parser.parse_args()
    run_session(SessionId.parse(arguments.session_id), arguments.intent_dir)


if __name__ == "__main__":
    main()
