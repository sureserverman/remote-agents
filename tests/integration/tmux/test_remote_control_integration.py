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


async def test_enable_treats_a_clean_claude_prompt_as_inactive_then_verifies_active():
    session_id = SessionId.new()
    gateway = Gateway(session_id)
    terminal = TmuxTerminal(gateway, {}, {}, startup_timeout=1)

    state = await terminal.remote_control(session_id, RemoteControlState.ACTIVE)

    assert state is RemoteControlState.ACTIVE
    assert gateway.sent == [(session_id, ("/remote-control", "Enter"))]
