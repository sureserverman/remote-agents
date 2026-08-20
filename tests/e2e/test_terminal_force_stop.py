"""Force stop rechecks current ownership before removing an exact tmux target."""

from test_terminal_launch import STARTUP_BUDGET, make_terminal

from remote_agents.domain.models import ProfileId, ProjectId, SessionId


async def test_force_stop_removes_only_a_currently_owned_exact_session(tmp_path):
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    session_id = SessionId.new()
    try:
        assert (await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))).live

        stopped = await terminal.force_stop(session_id)

        assert stopped.live is False
        assert await terminal.inspect(session_id) is None
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass
