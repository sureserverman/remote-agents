"""A resumed agent proves itself by its pane, because it reprints no banner."""

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.codec import ManagedPane
from remote_agents.adapters.tmux.gateway import TmuxInventory
from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_EXECUTABLE = "/usr/bin/claude"


def _profile(marker: str | None, blockers: tuple[str, ...] = ()) -> LaunchProfile:
    return LaunchProfile(
        _EXECUTABLE, (_EXECUTABLE, "--resume", "x"), {}, marker, ("C-c",), blockers
    )


class Gateway:
    """A live pane whose capture is whatever a resumed agent happens to be drawing."""

    def __init__(self, session_id: SessionId, capture: str, intent_directory: Path) -> None:
        self._session_id = session_id
        self._capture = capture
        self.intent_directory = intent_directory

    async def inventory(self) -> TmuxInventory:
        return TmuxInventory(
            (
                ManagedPane(
                    f"ra-{self._session_id}",
                    "%1",
                    True,
                    self._session_id,
                    ProjectId("opaque-editor"),
                    ProfileId("claude"),
                    100,
                    True,
                    False,
                ),
            ),
            (),
        )

    async def capture(self, _session_id: SessionId) -> str:
        return self._capture

    async def launch(self, *_args: object) -> None:
        return None


def test_a_profile_may_omit_a_marker_but_never_supply_an_empty_one() -> None:
    _profile(None)
    with pytest.raises(ValueError):
        _profile("")


async def _resume(tmp_path: Path, profile: LaunchProfile, capture: str):
    session_id = SessionId.new()
    project = tmp_path / "opaque-editor"
    project.mkdir()
    terminal = TmuxTerminal(
        Gateway(session_id, capture, tmp_path),
        {ProjectId("opaque-editor"): project},
        {},
        startup_timeout=0.2,
        resume_profile_factories={ProfileId("claude"): lambda _s, _c: profile},
    )
    return await terminal._launch_profile(
        session_id, ProjectId("opaque-editor"), ProfileId("claude"), profile
    )


@pytest.mark.asyncio
async def test_a_restored_conversation_is_ready_without_the_launch_banner(tmp_path) -> None:
    """The opaque-editor case: working pane, no banner anywhere, previously marked failed."""
    restored = "Running Stage 2 gate...\n  Task 2.1: Exported share target\n"
    assert "Claude Code" not in restored

    observation = await _resume(tmp_path, _profile(None), restored)

    assert observation.live
    assert observation.detail == ""


@pytest.mark.asyncio
async def test_a_blocker_still_denies_readiness_without_a_marker(tmp_path) -> None:
    """Dropping the marker must not drop the evidence that says *not* ready."""
    profile = _profile(None, ("Accessing workspace:",))

    observation = await _resume(tmp_path, profile, "Accessing workspace: /home/user\n")

    assert not observation.live
    assert observation.detail == "startup_timeout"


@pytest.mark.asyncio
async def test_a_launched_profile_still_has_to_show_its_banner(tmp_path) -> None:
    """Fresh launches keep the stronger evidence; only resume was unable to give it."""
    observation = await _resume(tmp_path, _profile("Claude Code"), "some other output\n")

    assert not observation.live
    assert observation.detail == "startup_timeout"
