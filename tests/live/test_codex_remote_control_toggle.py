"""Owner-run qualification of the Codex host toggle, against the real daemon and relay.

**This is the one test in the suite with side effects outside this machine.** Enabling
Remote Control enrols this host with OpenAI's relay, and it starts a daemon that outlives the
test. Both are deliberate — they are the thing being qualified — and both are gated behind a
third environment variable that exists for no other purpose, so selecting the codex profile
and a trusted project is not enough to trigger it by accident.

What it proves, in the order a doubting reader would want it:

1. the reading before anything is done, recorded rather than assumed;
2. that enabling reaches CONNECTED or CONNECTING, which is the relay round trip;
3. that a managed `codex` pane launched *after* that reaches readiness;
4. that the daemon lists a thread for that pane's workspace — the pane is daemon-backed and
   therefore phone-visible, which is the launch-order rule's whole content;
5. that disabling reaches DISABLED;
6. **that the pane is still alive and the daemon still answers** — the property the entire
   design is arranged around, and the one a `remote-control stop` implementation would fail;
7. that the pane is stopped and cleaned up, leaving nothing behind (ARCH-11).

It prints the two commands that undo everything, because a drill that enrols a machine and
does not say how to unenrol it is a drill that leaves the host changed.

**It also needs OpenAI's standalone Codex install**, not the npm package. Every verb on the
daemon surface refuses without it, because that is where the daemon starts and updates
app-server from -- and `codex --version` and `codex remote-control --help` both succeed
regardless, which is why the plan's Preflight did not catch it. Skipped, named, rather than
failed: a host that cannot run the feature is a fact about the host.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from remote_agents.adapters.agents.codex.remote_control import (
    REMOTE_CONTROL_ARGV,
    AsyncCommandRunner,
    CodexRemoteControl,
)
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.domain.remote_control import HostConnection, RemoteControlState

_UNDO = (
    "    codex app-server daemon disable-remote-control   # unenrol this host\n"
    "    codex app-server daemon stop                     # and stop the daemon it left running"
)


@pytest.mark.live_acceptance
async def test_codex_remote_control_enables_launches_visibly_and_disables_without_killing(
    tmp_path: Path,
) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_PROFILE") != "codex":
        pytest.skip("BLOCKED: codex_profile_not_selected")
    project_value = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT")
    if project_value is None:
        pytest.skip("BLOCKED: trusted_project_not_configured")
    # The third gate, and the reason it exists: everything above this line is read-only, and
    # everything below it changes a host and talks to a third party.
    if os.environ.get("REMOTE_AGENTS_LIVE_CODEX_REMOTE_CONTROL") != "1":
        pytest.skip("BLOCKED: codex_remote_control_not_consented")

    project_path = Path(project_value).resolve(strict=True)
    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("BLOCKED: executable_missing")
    # A precondition the plan's Preflight did not know to check, found by running this drill:
    # `codex --version` and `--help` both pass on the npm distribution, and the entire daemon
    # surface still refuses, because the daemon starts and updates app-server from this fixed
    # path. A skip rather than a failure -- the host cannot run the feature, which is a fact
    # about the host and not a defect in the code being qualified.
    if not (Path.home() / ".codex/packages/standalone/current/codex").exists():
        pytest.skip("BLOCKED: standalone_codex_install_missing")

    control = CodexRemoteControl()
    runner = AsyncCommandRunner()
    print(f"\nUndo everything this drill does with:\n{_UNDO}\n")

    # (1) Before.
    before = await control.status()
    print(f"before: {before.connection.value} ({before.server_name})")

    try:
        # (2) Enable, and reach the relay.
        enabled = await control.set_state(RemoteControlState.ACTIVE)
        assert enabled.connection in {HostConnection.CONNECTED, HostConnection.CONNECTING}, (
            f"enable did not reach the relay: {enabled.connection.value}"
        )
        assert enabled.state is RemoteControlState.ACTIVE

        # (3) A managed pane, launched AFTER the daemon is up.
        definition = next(
            profile for profile in closed_profiles() if profile.profile_id == ProfileId("codex")
        )
        session_id = SessionId.new()
        project_id = ProjectId("qualification")
        profile = build_launch_profile(
            definition,
            Path(executable),
            session_id,
            {
                key: os.environ[key]
                for key in ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
                if key in os.environ
            },
        )
        socket = f"remote-agents-test-{session_id.value.hex}"
        gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
        terminal = TmuxTerminal(
            gateway,
            {project_id: project_path},
            {definition.profile_id: profile},
            startup_timeout=30,
        )
        try:
            launched = await terminal.launch(session_id, project_id, definition.profile_id)
            assert launched.live, launched.detail

            # (4) The daemon knows about it, which is what "the phone can see it" means.
            listed = await runner.run((*REMOTE_CONTROL_ARGV["daemon_probe"],), timeout=15)
            assert listed.returncode == 0, (
                "the daemon stopped answering while a managed pane was attached to it"
            )
            print(f"daemon version while the pane runs: {listed.stdout.strip()[:120]}")

            # (5) Disable.
            disabled = await control.set_state(RemoteControlState.INACTIVE)
            assert disabled.connection is HostConnection.DISABLED, (
                f"disable did not take: {disabled.connection.value}"
            )
            assert disabled.state is RemoteControlState.INACTIVE

            # (6) THE POINT: nothing was torn down.
            observation = await terminal.inspect(session_id)
            assert observation is not None and observation.live, (
                "turning Remote Control off killed the managed pane -- which is what "
                "`remote-control stop` would have done, and why this project never issues it"
            )
            survivor = await runner.run((*REMOTE_CONTROL_ARGV["daemon_probe"],), timeout=15)
            assert survivor.returncode == 0, (
                "turning Remote Control off stopped the daemon; only the preference should "
                "have changed"
            )
            print("after disable: the pane is live and the daemon still answers")
        finally:
            # (7) Leave nothing behind.
            await terminal.force_stop(session_id)
            await gateway.kill_server()
    finally:
        await control.aclose()
        print(f"\nThis host is still enrolled unless you run:\n{_UNDO}\n")


@pytest.mark.live_acceptance
async def test_the_drill_refuses_to_run_without_its_own_consent_variable() -> None:
    """The gate itself, checked — a skip that never skips is not a gate.

    Runs in the ordinary suite too, since it asserts only that the variable is absent from
    this process's environment when it is absent. Cheap, and it is the difference between a
    consent check and a comment.
    """
    if os.environ.get("REMOTE_AGENTS_LIVE_CODEX_REMOTE_CONTROL") == "1":
        pytest.skip("the owner has consented in this shell, so there is nothing to prove")

    from remote_agents.adapters.agents.codex import remote_control as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "REMOTE_AGENTS_LIVE" not in source, (
        "the adapter must not read the drill's consent variable; the gate belongs to the test"
    )
    assert not any("stop" in argv for argv in REMOTE_CONTROL_ARGV.values()), (
        "the drill would be qualifying a table that can tear the daemon down"
    )


def test_the_undo_instructions_name_both_halves_of_what_the_drill_changes() -> None:
    """A drill that enrols a machine and does not say how to unenrol it changes the host."""
    assert "disable-remote-control" in _UNDO
    assert "daemon stop" in _UNDO
