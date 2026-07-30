"""Profile-defined graceful stop retains a dead pane until explicit cleanup."""

from test_terminal_launch import make_terminal

from remote_agents.domain.models import ProfileId, ProjectId, SessionId


async def test_graceful_stop_preserves_then_explicit_cleanup_removes_the_pane(tmp_path):
    terminal, gateway = make_terminal(tmp_path, timeout=0.3)
    session_id = SessionId.new()
    try:
        launched = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))
        assert launched.live is True

        stopped = await terminal.graceful_stop(session_id, ProfileId("fake"))
        assert stopped.preserved is True
        assert (await terminal.inspect(session_id)).preserved is True

        await terminal.cleanup(session_id)
        assert await terminal.inspect(session_id) is None
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass
