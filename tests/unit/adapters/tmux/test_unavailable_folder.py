"""An unavailable target refuses before any pane or intent exists."""

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId


class NeverStartedGateway:
    def __init__(self, root: Path):
        self.intent_directory = root / "intents"
        self.started = 0

    async def launch(self, *_args):
        self.started += 1
        raise AssertionError("an unavailable folder must not reach tmux")


@pytest.mark.parametrize("operation", ["launch", "resume"])
@pytest.mark.parametrize("target", ["missing", "file", "symlink-loop", "unknown-project"])
async def test_unavailable_folder_is_a_named_pre_pane_refusal(tmp_path, operation, target):
    folder = tmp_path / "project"
    if target == "file":
        folder.write_text("not a directory")
    elif target == "symlink-loop":
        folder.symlink_to(folder.name)
    project, agent = ProjectId("fixture-project"), ProfileId("fixture-agent")
    gateway = NeverStartedGateway(tmp_path)
    profile = LaunchProfile("/bin/true", ("/bin/true",), {}, None, ("C-c",))
    terminal = TmuxTerminal(
        gateway,
        {} if target == "unknown-project" else {project: folder},
        {},
        profile_factories={agent: lambda *_args: profile},
        resume_profile_factories={agent: lambda *_args: profile},
        startup_timeout=0,
    )
    sid = SessionId.new()
    result = (
        await terminal.launch(sid, project, agent)
        if operation == "launch"
        else await terminal.resume(sid, project, agent, "fixture-conversation")
    )
    assert result.detail == "folder_unavailable"
    assert not result.live
    assert gateway.started == 0
    assert not gateway.intent_directory.exists()
