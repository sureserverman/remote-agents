"""Dedicated-socket startup readiness outcomes using harmless fake agents."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.tmux import runtime
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import ProfileDefinition
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

# Startup budgets are split by what the test asserts, because only one direction is
# load-sensitive. `_launch_profile` polls until its deadline and returns the moment the
# readiness marker appears, so a *generous* budget costs nothing when the agent does become
# ready -- and a tight one is a race the machine can lose under load (BL-017). A test
# asserting the agent never becomes ready is the opposite: the budget IS the wait, load can
# only make it give up later, and it cannot be made to wrongly pass. So:
#
#   STARTUP_BUDGET   -- the agent will reach READY; the budget is a safety net, not a subject
#   TIMEOUT_FIRES    -- the timeout firing is what the test asserts; must stay shorter than
#                       the fake agent's 0.05s delay, and is safe because 0.05 > 0.01 always
#   NEVER_READY      -- nothing will ever satisfy readiness; the budget bounds giving up
#   READINESS_UNUSED -- the test never consults readiness at all (it asserts the written
#                       intent, which lands before the loop starts), so the budget only
#                       decides how long the test wastes before returning
STARTUP_BUDGET = 5.0
TIMEOUT_FIRES = 0.01
NEVER_READY = 0.05
READINESS_UNUSED = 0.05


@pytest.mark.parametrize(
    ("mode", "expected_live", "budget"),
    (
        ("ready", True, STARTUP_BUDGET),
        ("delayed", True, STARTUP_BUDGET),
        ("immediate_exit", False, NEVER_READY),
        ("invalid_intent", False, NEVER_READY),
    ),
)
async def test_terminal_launch_reports_real_readiness(
    tmp_path: Path, mode: str, expected_live: bool, budget: float
) -> None:
    terminal, gateway = make_terminal(tmp_path, timeout=budget, mode=mode)
    session_id = SessionId.new()
    try:
        if mode == "invalid_intent":
            terminal.invalidate_next_intent = True

        observation = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))

        assert observation.live is expected_live
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_terminal_launch_times_out_without_claiming_readiness(tmp_path: Path) -> None:
    terminal, gateway = make_terminal(tmp_path, timeout=TIMEOUT_FIRES, mode="delayed")
    session_id = SessionId.new()
    try:
        observation = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))

        assert observation.live is False
        assert observation.detail == "startup_timeout"
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_a_link_planted_at_the_intent_name_refuses_the_launch(tmp_path: Path) -> None:
    """O_NOFOLLOW refusing has to become an observation, not an exception out through the bot.

    The directory guard above this already answers `invalid_intent` for its own class of
    failure; the file open was the one site in this path that still refused by raising, and
    nothing between here and the Telegram handler would have caught it -- leaving the record
    STARTING for reconciliation to find and no launch behind it.
    """
    terminal, _ = make_terminal(tmp_path, timeout=NEVER_READY)
    session_id = SessionId.new()
    intents = tmp_path / "intents"
    intents.mkdir(mode=0o700, parents=True, exist_ok=True)
    (intents / f"{session_id}.json").symlink_to(tmp_path / "elsewhere.json")

    observation = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))

    assert observation.live is False
    assert observation.detail == "invalid_intent"
    # The link is what it was: refusing means this frame wrote nothing at all, here or at
    # the target the link named.
    assert (intents / f"{session_id}.json").is_symlink()
    assert not (tmp_path / "elsewhere.json").exists()


async def test_an_intent_is_owner_only_before_its_contents_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch environment and argv must never exist in a file anyone else can read.

    `O_CREAT`'s mode applies only when it creates the file, so an intent left behind at a
    looser mode by an older build kept it -- which the code already knew and repaired with a
    `chmod`. The repair ran *after* the write, so the window it was closing was exactly the
    one in which the document was on disk. Observed at the descriptor, which is the only
    place the ordering is visible: by the time the file has been written it reads 0600 either
    way.
    """
    terminal, _ = make_terminal(tmp_path, timeout=NEVER_READY, mode="immediate_exit")
    session_id = SessionId.new()
    intents = tmp_path / "intents"
    intents.mkdir(mode=0o700, parents=True, exist_ok=True)
    stale = intents / f"{session_id}.json"
    stale.write_text("{}", encoding="utf-8")
    stale.chmod(0o644)

    seen: list[int] = []
    real_fdopen = os.fdopen

    def record_mode_at_open(descriptor: int, *args: object, **kwargs: object):
        seen.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return real_fdopen(descriptor, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime.os, "fdopen", record_mode_at_open)

    await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))

    assert seen == [0o600]
    assert stat.S_IMODE(stale.stat().st_mode) == 0o600


async def test_terminal_rechecks_a_timed_out_launch_before_recovering_it(tmp_path: Path) -> None:
    terminal, gateway = make_terminal(tmp_path, timeout=TIMEOUT_FIRES, mode="delayed")
    session_id = SessionId.new()
    try:
        launched = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))
        assert not launched.live
        assert not (await terminal.confirm_ready(session_id, ProfileId("fake"))).live

        # Waiting on the condition, not on a clock. This was `asyncio.sleep(0.06)` -- chosen
        # to be just past the fake agent's 0.05s delay -- which is the whole margin the test
        # had. Under load the agent needs longer than 60ms to print READY and the recheck
        # below saw `not_ready`: 7 failures in 12 runs of the e2e tree at 2x CPU
        # oversubscription. `confirm_ready` is single-shot, so the polling belongs here.
        #
        # This does weaken the test's temporal guarantee, and that is a deliberate trade
        # rather than a side effect of renaming a constant: it used to assert recovery
        # within ~60ms and now asserts it within the polling bound. What the test is for --
        # that a timed-out launch is rechecked rather than written off -- is unchanged, and
        # the old guarantee was not one the machine could actually keep.
        assert await settle_ready(terminal, session_id, ProfileId("fake"))
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_terminal_builds_a_profile_for_the_actual_generated_session(tmp_path: Path) -> None:
    agent = tmp_path / "fake_agent.py"
    agent.write_text("import time\nprint('READY', flush=True)\ntime.sleep(1)\n", encoding="utf-8")
    session_id = SessionId.new()
    created_for: list[SessionId] = []

    def profile_factory(received_session_id: SessionId) -> LaunchProfile:
        created_for.append(received_session_id)
        return LaunchProfile(
            sys.executable,
            (sys.executable, str(agent), f"ra-{received_session_id}"),
            {"PATH": os.environ["PATH"]},
            "READY",
        )

    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )
    terminal = TmuxTerminal(
        gateway,
        {ProjectId("opaque-editor"): tmp_path},
        {},
        startup_timeout=STARTUP_BUDGET,
        profile_factories={ProfileId("fake"): profile_factory},
    )
    try:
        launched = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))

        assert launched.live
        assert created_for == [session_id]
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_the_written_intent_carries_the_session_environment(tmp_path: Path) -> None:
    """The hook reads its session from the environment, and the intent is what sets it.

    The pane is started by re-executing the intent document, so a variable that reaches
    `LaunchProfile.environment` but not this file never reaches the agent at all.
    """
    session_id = SessionId.new()

    def profile_factory(received_session_id: SessionId) -> LaunchProfile:
        return build_launch_profile(
            ProfileDefinition(
                ProfileId("claude"), "claude", ("claude",), ("--version",), ("/exit", "Enter")
            ),
            Path(sys.executable),
            received_session_id,
            {"PATH": os.environ["PATH"]},
        )

    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )
    terminal = TmuxTerminal(
        gateway,
        {ProjectId("opaque-editor"): tmp_path},
        {},
        startup_timeout=READINESS_UNUSED,
        profile_factories={ProfileId("claude"): profile_factory},
    )
    try:
        await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("claude"))

        written = json.loads(
            (tmp_path / "intents" / f"{session_id}.json").read_text(encoding="utf-8")
        )
        assert written["environment"][SESSION_ID_VARIABLE] == str(session_id)
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def settle_ready(
    terminal: TmuxTerminal,
    session_id: SessionId,
    profile_id: ProfileId,
    *,
    tries: int = 200,
) -> bool:
    """Poll `confirm_ready` until the pane reports live, or the tries run out.

    The counterpart to `settle()` in `test_tui_snapshots.py`, and it exists for the same
    reason: a single wall-clock wait encodes a guess about how long another process needs,
    and the guess is what the machine's load invalidates. `confirm_ready` is a single-shot
    check by design -- it answers "is it ready *now*" -- so a caller that wants "is it ready
    *yet*" has to do the polling, and doing it here keeps that out of every test.

    200 tries is a bound rather than a hang if readiness never arrives. The wall-clock
    ceiling is *at least* 2s and in practice more: each iteration also runs `confirm_ready`,
    which costs two tmux round-trips (`list-panes` then `capture-pane`) before the 10ms
    sleep. Deliberately not stated as a flat "2s" -- that would be the same kind of
    arithmetic-shaped claim that is only true if you ignore the work between the sleeps.
    """
    for _ in range(tries):
        if (await terminal.confirm_ready(session_id, profile_id)).live:
            return True
        await asyncio.sleep(0.01)
    return False


def make_terminal(
    tmp_path: Path, *, timeout: float, mode: str = "ready"
) -> tuple[TmuxTerminal, TmuxGateway]:
    agent = tmp_path / "fake_agent.py"
    agent.write_text(
        "import sys, time\n"
        "if sys.argv[1] == 'delayed': time.sleep(0.05)\n"
        "if sys.argv[1] != 'immediate_exit': print('READY', flush=True); time.sleep(1)\n",
        encoding="utf-8",
    )
    socket = f"remote-agents-test-{uuid4().hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
    profile = LaunchProfile(
        sys.executable,
        (sys.executable, str(agent), mode),
        {"PATH": os.environ["PATH"]},
        "READY",
    )
    terminal = TmuxTerminal(
        gateway,
        {ProjectId("opaque-editor"): tmp_path},
        {ProfileId("fake"): profile},
        startup_timeout=timeout,
    )
    return terminal, gateway
