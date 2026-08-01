"""Fixed graceful sequences must reach a TUI as distinct ordered key events."""

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        return ""


@pytest.mark.asyncio
async def test_fixed_key_sequence_is_sent_as_ordered_events(monkeypatch) -> None:
    runner = RecordingRunner()
    gateway = TmuxGateway("remote-agents", runner)
    delays: list[float] = []

    async def record_delay(value: float) -> None:
        delays.append(value)

    monkeypatch.setattr("remote_agents.adapters.tmux.gateway.asyncio.sleep", record_delay)
    session_id = SessionId.new()

    await gateway.send_keys(session_id, ("/quit", "Enter", "Enter"))

    target = f"ra-{session_id}:"
    assert runner.calls == [
        ("tmux", "-L", "remote-agents", "send-keys", "-t", target, "/quit"),
        ("tmux", "-L", "remote-agents", "send-keys", "-t", target, "Enter"),
        ("tmux", "-L", "remote-agents", "send-keys", "-t", target, "Enter"),
    ]
    assert delays == [0.15, 0.15]
