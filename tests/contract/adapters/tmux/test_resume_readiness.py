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


async def _confirm(tmp_path: Path, profile: LaunchProfile, capture: str):
    """`confirm_ready` against the same fixed pane the resume helper uses."""
    session_id = SessionId.new()
    project = tmp_path / "opaque-editor"
    project.mkdir(exist_ok=True)
    terminal = TmuxTerminal(
        Gateway(session_id, capture, tmp_path),
        {ProjectId("opaque-editor"): project},
        {ProfileId("claude"): profile},
        startup_timeout=0.2,
    )
    return await terminal.confirm_ready(session_id, ProfileId("claude"))


@pytest.mark.asyncio
async def test_a_recheck_reports_the_dialog_rather_than_a_bare_not_ready(tmp_path) -> None:
    """The recheck is what a late-observed dialog is corrected from, so it must say why.

    `refresh_readiness` and reconciliation both call this to decide whether a record that
    reads FAILED should be promoted. "not_ready" cannot distinguish an agent that is slow
    from one that is stopped on a question, and those two want opposite repairs -- wait, and
    ask the owner.
    """
    profile = _profile("Claude Code", ("Accessing workspace:",))

    observation = await _confirm(tmp_path, profile, "Claude Code\nAccessing workspace: /x\n")

    assert observation.awaiting_trust


@pytest.mark.asyncio
async def test_a_recheck_of_a_genuinely_slow_agent_is_still_a_bare_not_ready(tmp_path) -> None:
    profile = _profile("Claude Code", ("Accessing workspace:",))

    observation = await _confirm(tmp_path, profile, "still thinking\n")

    assert not observation.live
    assert not observation.awaiting_trust
    assert observation.detail == "not_ready"


@pytest.mark.asyncio
async def test_a_restored_conversation_is_ready_without_the_launch_banner(tmp_path) -> None:
    """The opaque-editor case: working pane, no banner anywhere, previously marked failed."""
    restored = "Running Stage 2 gate...\n  Task 2.1: Exported share target\n"
    assert "Claude Code" not in restored

    observation = await _resume(tmp_path, _profile(None), restored)

    assert observation.live
    assert observation.detail == ""


@pytest.mark.asyncio
async def test_a_blocker_answers_the_launch_instead_of_waiting_out_the_budget(tmp_path) -> None:
    """A blocker used to mean "keep waiting". It means "this is the answer" now.

    Dropping the marker still does not drop the evidence that says *not ready* -- what
    changed is what that evidence is worth. A blocker string is the agent saying it is
    stopped on a question, and no amount of further waiting changes that, so the budget was
    being spent to arrive at a conclusion the first capture already supported.
    """
    profile = _profile(None, ("Accessing workspace:",))

    observation = await _resume(tmp_path, profile, "Accessing workspace: /home/user\n")

    assert observation.awaiting_trust
    assert observation.live
    assert observation.detail == ""


@pytest.mark.asyncio
async def test_the_dialog_alone_answers_a_profile_that_may_be_asked(tmp_path) -> None:
    """The blocker table is not the only evidence; the dialog itself counts for claude.

    `claude`'s configured blocker is the *pre-trust* screen ("Accessing workspace:"), so a
    pane that has already drawn the question and is resting on it matches no blocker at all.
    Classifying the capture is what closes that gap, and it is gated on TRUST_ANSWERABLE
    because it is the same classifier the answering path uses.
    """
    profile = _profile(None, ())
    capture = (
        "Is this a project you created or one you trust?\n"
        "\n"
        "\u276f No, exit\n"
        "  Yes, I trust this folder\n"
    )

    observation = await _resume(tmp_path, profile, capture)

    assert observation.awaiting_trust
    assert observation.live


@pytest.mark.asyncio
async def test_an_ordinary_ready_pane_is_not_awaiting_trust(tmp_path) -> None:
    observation = await _resume(tmp_path, _profile(None), "Running Stage 2 gate...\n")

    assert observation.live
    assert not observation.awaiting_trust


@pytest.mark.asyncio
async def test_a_launched_profile_that_never_shows_its_banner_is_still_a_timeout(
    tmp_path,
) -> None:
    """The new early return must not swallow the ordinary failure it sits beside."""
    observation = await _resume(tmp_path, _profile("Claude Code"), "some other output\n")

    assert not observation.live
    assert not observation.awaiting_trust
    assert observation.detail == "startup_timeout"


@pytest.mark.asyncio
async def test_a_launched_profile_still_has_to_show_its_banner(tmp_path) -> None:
    """Fresh launches keep the stronger evidence; only resume was unable to give it."""
    observation = await _resume(tmp_path, _profile("Claude Code"), "some other output\n")

    assert not observation.live
    assert observation.detail == "startup_timeout"
