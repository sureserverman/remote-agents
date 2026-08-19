"""A target the terminal destroyed on its own is evidence, not an opaque failure."""

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.domain.models import SessionId
from remote_agents.ports.terminal import TerminalTargetMissing

# The exact stderr tmux writes for each case, so retyping stays pinned to real output
# rather than to a paraphrase of it.
MISSING_SESSION = "tmux command failed: can't find session: ra-{session_id}"
ABSENT_SERVER = (
    "tmux command failed: error connecting to /tmp/tmux-1000/remote-agents "
    "(No such file or directory)"
)
BROKEN_TMUX = "tmux command failed: usage: capture-pane [-aepPqCJN]"


class FailingRunner:
    def __init__(self, message: str) -> None:
        self.message = message

    async def run(self, *argv: str) -> str:
        raise RuntimeError(self.message.format(session_id=argv[-1].rstrip(":").removeprefix("ra-")))


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [MISSING_SESSION, ABSENT_SERVER])
async def test_capture_of_a_target_that_is_gone_is_reported_as_missing(message: str) -> None:
    gateway = TmuxGateway("remote-agents", FailingRunner(message))

    with pytest.raises(TerminalTargetMissing):
        await gateway.capture(SessionId.new())


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [MISSING_SESSION, ABSENT_SERVER])
async def test_killing_a_target_that_is_gone_is_reported_as_missing(message: str) -> None:
    gateway = TmuxGateway("remote-agents", FailingRunner(message))

    with pytest.raises(TerminalTargetMissing):
        await gateway.mutate("kill-session", f"ra-{SessionId.new()}")


@pytest.mark.asyncio
async def test_a_broken_tmux_is_never_mistaken_for_a_target_that_ended() -> None:
    """A failure that says nothing about the target must keep its own type.

    Retyping this one would let any tmux fault end a live session's record.
    """
    gateway = TmuxGateway("remote-agents", FailingRunner(BROKEN_TMUX))

    with pytest.raises(RuntimeError) as raised:
        await gateway.capture(SessionId.new())
    assert not isinstance(raised.value, TerminalTargetMissing)


class GoneGateway:
    """A gateway whose pane died before cleanup was asked to remove it."""

    def __init__(self, intent_directory: Path) -> None:
        self.intent_directory = intent_directory
        self.attempted = False

    async def destroy(self, session_id: SessionId) -> str:
        self.attempted = True
        raise TerminalTargetMissing(f"managed target is gone: ra-{session_id}")


@pytest.mark.asyncio
async def test_cleanup_of_an_already_dead_pane_still_removes_local_state(tmp_path: Path) -> None:
    gateway = GoneGateway(tmp_path)
    terminal = TmuxTerminal(gateway, {}, {}, startup_timeout=1)
    session_id = SessionId.new()
    intent = tmp_path / f"{session_id}.json"
    intent.write_text("{}", encoding="utf-8")

    await terminal.cleanup(session_id)

    assert gateway.attempted
    assert not intent.exists()
