"""Managed tmux launch accepts only fixed runner arguments and trusted metadata."""

from pathlib import Path

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import ProfileId, ProjectId, SessionId


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        return ""


async def test_launch_uses_only_fixed_runner_and_opaque_session_identifier(tmp_path: Path) -> None:
    runner = RecordingRunner()
    gateway = TmuxGateway("remote-agents", runner)
    session_id = SessionId.new()

    await gateway.launch(session_id, ProjectId("opaque-editor"), ProfileId("claude"), tmp_path)

    new_session = runner.calls[0]
    assert new_session[:8] == (
        "tmux",
        "-L",
        "remote-agents",
        "new-session",
        "-d",
        "-s",
        f"ra-{session_id}",
        "-c",
    )
    assert new_session[-5:-1] == (
        "-m",
        "remote_agents.adapters.tmux.session_runner",
        str(session_id),
        "--intent-dir",
    )
    assert any("remain-on-exit" in call for call in runner.calls)
    assert any("@remote_agents_schema" in call for call in runner.calls)
