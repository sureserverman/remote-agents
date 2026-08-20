"""Ownership-safe dedicated-socket tmux inventory contract."""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.codec import PANE_FORMAT, exact_session_target
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import ProfileId, ProjectId, SessionId


class RecordingRunner:
    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        return self.output


def pane_line(session_id: SessionId, *, schema: str = "1") -> str:
    return "|".join(
        (
            f"ra-{session_id}",
            "$1",
            "%1",
            "100",
            "0",
            "0",
            schema,
            str(session_id),
            "opaque-editor",
            "claude",
        )
    )


async def test_inventory_uses_only_the_configured_socket_and_quarantines_bad_tags() -> None:
    session_id = SessionId.new()
    runner = RecordingRunner(f"{pane_line(session_id)}\n{pane_line(SessionId.new(), schema='3')}\n")
    gateway = TmuxGateway("remote-agents", runner)

    inventory = await gateway.inventory()

    assert inventory.managed[0].session_id == session_id
    assert inventory.managed[0].process_id == 100
    assert inventory.orphans[0].reason == "tmux management schema is missing or unsupported"
    assert runner.calls == [("tmux", "-L", "remote-agents", "list-panes", "-a", "-F", PANE_FORMAT)]


@pytest.mark.parametrize("socket", ("", "default", "other-server"))
def test_gateway_rejects_default_or_untrusted_socket_before_runner_use(socket: str) -> None:
    with pytest.raises(ValueError, match="dedicated socket"):
        TmuxGateway(socket, RecordingRunner())


async def test_a_name_that_is_not_a_managed_session_never_reaches_a_subprocess() -> None:
    """The half of the old `mutate` guard that still has something to guard.

    `mutate` took a verb and a target as free text, so it needed a closed allow-list for
    both. It is gone: every operation is a named method taking a typed `SessionId`, so a
    forbidden verb has nowhere to be written and a malformed name cannot be constructed. What
    remains worth asserting is the codec's own refusal, which is what the typing rests on —
    a name that is not a canonical managed session is rejected before any argv is built.
    """
    with pytest.raises(ValueError, match="canonical UUID"):
        exact_session_target("ra-prefix")
    with pytest.raises(ValueError, match="start with ra-"):
        exact_session_target("prefix")


async def test_gateway_never_exposes_resume_arguments_to_the_tmux_command_boundary(
    tmp_path,
) -> None:
    runner = RecordingRunner()
    gateway = TmuxGateway("remote-agents", runner, intent_directory=tmp_path / "intents")
    session_id = SessionId.new()

    await gateway.launch(session_id, ProjectId("opaque-editor"), ProfileId("claude"), tmp_path)

    assert all("--resume" not in argument for call in runner.calls for argument in call)
