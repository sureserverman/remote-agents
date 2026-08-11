"""Session runner reloads one validated intent and execs only its curated argv."""

import json
import os
import subprocess
import sys
from pathlib import Path

from remote_agents.domain.models import SessionId
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE


def test_runner_executes_the_validated_intent_in_its_configured_directory(tmp_path: Path) -> None:
    session_id = SessionId.new()
    intent_dir = tmp_path / "intents"
    intent_dir.mkdir()
    marker = tmp_path / "ran.txt"
    agent = tmp_path / "fake_agent.py"
    agent.write_text(
        "from pathlib import Path\nPath('ran.txt').write_text('ok')\n", encoding="utf-8"
    )
    (intent_dir / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": str(session_id),
                "profile_id": "fake-agent",
                "executable": sys.executable,
                "argv": [sys.executable, str(agent)],
                "cwd": str(tmp_path),
                "environment": {"PATH": os.environ["PATH"]},
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "remote_agents.adapters.tmux.session_runner",
            str(session_id),
            "--intent-dir",
            str(intent_dir),
        ],
        check=True,
    )

    assert marker.read_text(encoding="utf-8") == "ok"


def test_runner_hands_the_session_environment_to_the_process_it_becomes(tmp_path: Path) -> None:
    """Close the last leg: the variable must be in the agent's own environment, not just on disk.

    Everything upstream of `execvpe` is checked by reading the intent document, which proves
    only what was written. This is the one test that asks the started process itself, and it
    is worth its cost because that variable is the sole signal telling an installed hook
    whether the session it fired in belongs to this service.
    """
    session_id = SessionId.new()
    intent_dir = tmp_path / "intents"
    intent_dir.mkdir()
    observed = tmp_path / "observed.txt"
    agent = tmp_path / "fake_agent.py"
    agent.write_text(
        "import os\nfrom pathlib import Path\n"
        "Path('observed.txt').write_text(os.environ.get('REMOTE_AGENTS_SESSION_ID', '<unset>'))\n",
        encoding="utf-8",
    )
    (intent_dir / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": str(session_id),
                "profile_id": "fake-agent",
                "executable": sys.executable,
                "argv": [sys.executable, str(agent)],
                "cwd": str(tmp_path),
                "environment": {
                    "PATH": os.environ["PATH"],
                    SESSION_ID_VARIABLE: str(session_id),
                },
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "remote_agents.adapters.tmux.session_runner",
            str(session_id),
            "--intent-dir",
            str(intent_dir),
        ],
        check=True,
    )

    assert observed.read_text(encoding="utf-8") == str(session_id)
