from remote_agents.adapters.tmux.codec import ManagedPane
from remote_agents.adapters.tmux.gateway import TmuxInventory
from remote_agents.adapters.tmux.runtime import TmuxTerminal, _remote_control_state
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState


def test_unknown_capture_fails_closed_before_any_interaction_is_attempted():
    assert _remote_control_state("unrelated terminal output") is RemoteControlState.UNKNOWN


class Gateway:
    def __init__(self, session_id: SessionId) -> None:
        self.session_id = session_id
        self.sent = []
        self.capture_count = 0

    async def inventory(self):
        return TmuxInventory(
            (
                ManagedPane(
                    f"ra-{self.session_id}",
                    self.session_id,
                    ProjectId("opaque-editor"),
                    ProfileId("claude"),
                    100,
                    True,
                    False,
                ),
            ),
            (),
        )

    async def capture(self, _session_id):
        self.capture_count += 1
        return "Claude Code" if self.capture_count == 1 else "/remote-control is active"

    async def send_keys(self, session_id, keys):
        self.sent.append((session_id, keys))


async def test_enable_waits_for_claude_to_report_active_after_the_fixed_interaction(monkeypatch):
    session_id = SessionId.new()
    gateway = Gateway(session_id)
    terminal = TmuxTerminal(gateway, {}, {}, startup_timeout=1)
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr("remote_agents.adapters.tmux.runtime.asyncio.sleep", record_wait)

    state = await terminal.remote_control(session_id, RemoteControlState.ACTIVE)

    assert state is RemoteControlState.ACTIVE
    assert gateway.sent == [(session_id, ("/remote-control", "Enter"))]
    assert waits == [3]


async def test_disable_opens_the_remote_control_menu_before_disconnect(monkeypatch):
    session_id = SessionId.new()

    class ActiveGateway(Gateway):
        async def capture(self, _session_id):
            self.capture_count += 1
            return (
                "/remote-control is active"
                if self.capture_count == 1
                else "Remote Control disconnected."
            )

    gateway = ActiveGateway(session_id)
    terminal = TmuxTerminal(gateway, {}, {}, startup_timeout=1)
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr("remote_agents.adapters.tmux.runtime.asyncio.sleep", record_wait)

    state = await terminal.remote_control(session_id, RemoteControlState.INACTIVE)

    assert state is RemoteControlState.INACTIVE
    assert gateway.sent == [
        (session_id, ("/remote-control", "Enter")),
        (session_id, ("Up", "Up", "Enter")),
    ]
    assert waits == [1, 2]
