"""Ownership-safe dedicated-socket tmux inventory contract."""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.codec import PANE_FORMAT
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId


class RecordingRunner:
    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        return self.output


def pane_line(session_id: SessionId, *, schema: str = "1") -> str:
    return "\x1f".join(
        (f"ra-{session_id}", "$1", "%1", "0", "0", schema, str(session_id), "opaque-editor", "claude")
    )


async def test_inventory_uses_only_the_configured_socket_and_quarantines_bad_tags() -> None:
    session_id = SessionId.new()
    runner = RecordingRunner(f"{pane_line(session_id)}\n{pane_line(SessionId.new(), schema='2')}\n")
    gateway = TmuxGateway("remote-agents", runner)

    inventory = await gateway.inventory()

    assert inventory.managed[0].session_id == session_id
    assert inventory.orphans[0].reason == "tmux management schema is missing or unsupported"
    assert runner.calls == [("tmux", "-L", "remote-agents", "list-panes", "-a", "-F", PANE_FORMAT)]


@pytest.mark.parametrize("socket", ("", "default", "other-server"))
def test_gateway_rejects_default_or_untrusted_socket_before_runner_use(socket: str) -> None:
    with pytest.raises(ValueError, match="dedicated socket"):
        TmuxGateway(socket, RecordingRunner())


async def test_gateway_refuses_forbidden_or_prefix_mutations_before_subprocess() -> None:
    runner = RecordingRunner()
    gateway = TmuxGateway("remote-agents", runner)

    with pytest.raises(ValueError, match="forbidden"):
        await gateway.mutate("kill-server", "ra-any")
    with pytest.raises(ValueError, match="managed session"):
        await gateway.mutate("kill-session", "ra-prefix")

    assert runner.calls == []
