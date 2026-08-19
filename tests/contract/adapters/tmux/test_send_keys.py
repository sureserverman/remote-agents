"""Fixed graceful sequences must reach a TUI as distinct ordered key events."""

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId


class RecordingRunner:
    """Answers the resolution listing with a legacy pane at home, then records the keys.

    The listing matters because a sequence is aimed before it is typed: a schema-1 session
    resolves to no pane and keeps the session target it has always used, which is the shape
    this file asserts. Returning nothing at all would mean nothing claims the identity, and
    the gateway refuses to aim at a window it cannot show still holds the agent.
    """

    def __init__(self, session_id: SessionId) -> None:
        self._line = "|".join(
            (
                f"ra-{session_id}",
                "$1",
                "%1",
                "100",
                "0",
                "",
                "1",
                str(session_id),
                "opaque-editor",
                "claude",
            )
        )
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        return self._line if "list-panes" in argv else ""


@pytest.mark.asyncio
async def test_fixed_key_sequence_is_sent_as_ordered_events(monkeypatch) -> None:
    session_id = SessionId.new()
    runner = RecordingRunner(session_id)
    gateway = TmuxGateway("remote-agents", runner)
    delays: list[float] = []

    async def record_delay(value: float) -> None:
        delays.append(value)

    monkeypatch.setattr("remote_agents.adapters.tmux.gateway.asyncio.sleep", record_delay)

    await gateway.send_keys(session_id, ("/quit", "Enter", "Enter"))

    # Filtered to the key events, because the sequence is now preceded by one `list-panes`
    # that resolves which pane to type into. This test is about ordering and separation —
    # that each key arrives as its own event, with a gap — and the resolution call is a
    # different subject with its own test (`test_send_keys_resolves_once_for_the_whole_
    # sequence`). This runner answers the listing with "", so nothing resolves and the
    # session target is the fallback, which is the shape this test has always asserted.
    target = f"ra-{session_id}:"
    assert runner.calls[0][3] == "list-panes", "the sequence is preceded by one resolution"
    assert [call for call in runner.calls if "send-keys" in call] == [
        ("tmux", "-L", "remote-agents", "send-keys", "-t", target, "/quit"),
        ("tmux", "-L", "remote-agents", "send-keys", "-t", target, "Enter"),
        ("tmux", "-L", "remote-agents", "send-keys", "-t", target, "Enter"),
    ]
    assert delays == [0.15, 0.15]
