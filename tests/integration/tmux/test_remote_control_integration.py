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
                    "%1",
                    True,
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

    #: What the pane shows at each capture. Three now, where there were two: the disable path
    #: re-reads after asking for the menu and sends `Up, Up, Enter` only if *that* capture
    #: shows one. Measured on claude 2.1.269, those keys at a bare prompt are `history,
    #: history, submit` -- they started a real agent turn in a disposable pane. So the proof
    #: has to be the last thing read before them, which is what this sequence now models.
    _MENU = (
        "   Remote Control\n"
        "     Disconnect this session\n"
        "   ❯ Continue\n"
        "   Enter to select · Esc to continue\n"
    )

    class ActiveGateway(Gateway):
        async def capture(self, _session_id):
            self.capture_count += 1
            return {
                1: "/remote-control is active",
                2: _MENU,
            }.get(self.capture_count, "Remote Control disconnected.")

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


async def test_a_disable_never_sends_the_arrows_at_a_pane_showing_no_menu(monkeypatch):
    """The ordering this file is about, stated as the refusal it now is.

    The test above proves the arrows follow a menu. This one proves nothing follows its
    absence -- which is the half that matters, because `Up, Up, Enter` is only a disconnect
    while a menu is receiving it. At a prompt it recalls the owner's last message and submits
    it, and a disposable pane driven that way started a Claude turn that ran shell commands.
    """
    session_id = SessionId.new()

    class SilentGateway(Gateway):
        async def capture(self, _session_id):
            self.capture_count += 1
            return "/remote-control is active"

    gateway = SilentGateway(session_id)
    terminal = TmuxTerminal(gateway, {}, {}, startup_timeout=1)

    async def record_wait(seconds):
        del seconds

    monkeypatch.setattr("remote_agents.adapters.tmux.runtime.asyncio.sleep", record_wait)

    state = await terminal.remote_control(session_id, RemoteControlState.INACTIVE)

    assert gateway.sent == [(session_id, ("/remote-control", "Enter"))], (
        "asking for a menu is allowed; acting as though one appeared is not"
    )
    # **And the answer is what the pane says, not a flat UNKNOWN.** This assertion read
    # `is UNKNOWN` when it was written, which pinned a defect the gate's reviews then found:
    # the open-menu keys *are* the enable keys, so against a genuinely disconnected pane that
    # send turns Remote Control on -- and `set_remote_control_state` clears the record on
    # UNKNOWN, so the owner was told nothing had happened about a session that had just become
    # reachable from their phone. This gateway answers `/remote-control is active` throughout,
    # which is exactly what such a pane shows afterwards.
    assert state is RemoteControlState.ACTIVE
