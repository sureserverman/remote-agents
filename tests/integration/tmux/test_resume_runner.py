"""The fixed session runner executes a persisted provider-resume intent verbatim."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from remote_agents.domain.models import SessionId


def test_runner_executes_a_provider_resume_argv_without_a_shell(tmp_path: Path) -> None:
    session_id = SessionId.new()
    intent_dir = tmp_path / "intents"
    intent_dir.mkdir()
    observed = tmp_path / "argv.json"
    source_id = "provider;opaque-id"
    agent = tmp_path / "fake_agent.py"
    agent.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"Path({str(observed)!r}).write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    (intent_dir / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": str(session_id),
                "profile_id": "claude",
                "executable": sys.executable,
                "argv": [sys.executable, str(agent), "--resume", source_id],
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

    assert json.loads(observed.read_text(encoding="utf-8")) == ["--resume", source_id]
