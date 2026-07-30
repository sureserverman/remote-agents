"""Session runner reloads one validated intent and execs only its curated argv."""

import json
import os
import subprocess
import sys
from pathlib import Path

from remote_agents.domain.models import SessionId


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
