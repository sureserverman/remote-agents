"""Concrete dedicated-socket terminal adapter with bounded startup readiness."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from remote_agents.adapters.tmux.codec import attach_command
from remote_agents.adapters.tmux.gateway import TmuxGateway, TmuxRunner
from remote_agents.adapters.tmux.remote_control import (
    REMOTE_CONTROL_DISCONNECT_KEYS,
    REMOTE_CONTROL_ENABLE_KEYS,
    REMOTE_CONTROL_OPEN_MENU_KEYS,
    classify_remote_control_capture,
)
from remote_agents.adapters.tmux.trust import TRUST_KEYS, classify_trust_capture
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.trust import TRUST_ANSWERABLE, TrustState
from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.terminal import (
    GRACEFUL_TIMEOUT,
    UNKNOWN_SESSION,
    TerminalObservation,
    TerminalTargetMissing,
)

_REMOTE_CONTROL_ENABLE_WAIT_SECONDS = 3
_REMOTE_CONTROL_MENU_WAIT_SECONDS = 1
_REMOTE_CONTROL_DISABLE_WAIT_SECONDS = 2
# The dialog clears in one redraw; this is the pump's time to repaint, not the agent's time
# to think. Shorter than every remote-control wait above because nothing is being started --
# a keypress is being acknowledged.
_TRUST_ANSWER_WAIT_SECONDS = 1


class AsyncTmuxRunner(TmuxRunner):
    """Run only prevalidated tmux argument vectors without a shell."""

    async def run(self, *argv: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(
                f"tmux command failed: {stderr.decode('utf-8', errors='replace').strip()}"
            )
        return stdout.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    """Already-curated argv and environment for one adapter-resolved profile."""

    executable: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    readiness_marker: str | None
    """Text proving the agent finished starting, or None when its pane must prove it.

    A marker is a banner the agent prints once on a fresh start. A resumed agent redraws
    a restored conversation into the alternate screen buffer instead, so the banner is in
    neither the viewport nor the scrollback and no marker can ever match. Those profiles
    pass None and are judged by the pane, which is honest evidence here: an agent that
    fails to start exits, and an exited agent leaves a dead pane.
    """
    graceful_keys: tuple[str, ...] = ("C-c",)
    readiness_blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not Path(self.executable).is_absolute()
            or not self.argv
            or self.argv[0] != self.executable
            or self.readiness_marker == ""
        ):
            raise ValueError("profile executable and argv must be fixed and absolute")


class TmuxTerminal:
    """Resolve typed IDs locally, then report tmux observation rather than database liveness."""

    def __init__(
        self,
        gateway: TmuxGateway,
        project_paths: Mapping[ProjectId, Path],
        profiles: dict[ProfileId, LaunchProfile],
        *,
        startup_timeout: float,
        profile_factories: dict[ProfileId, Callable[[SessionId], LaunchProfile]] | None = None,
        resume_profile_factories: (
            dict[ProfileId, Callable[[SessionId, ProviderConversationId], LaunchProfile]] | None
        ) = None,
    ) -> None:
        self._gateway = gateway
        self._project_paths = project_paths
        self._profiles = profiles
        self._profile_factories = profile_factories or {}
        self._resume_profile_factories = resume_profile_factories or {}
        self._startup_timeout = startup_timeout
        self.invalidate_next_intent = False
        self._session_profiles: dict[SessionId, LaunchProfile] = {}

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Persist a resolved intent, launch it, then require observed pane liveness."""
        try:
            profile = self._profiles.get(profile_id)
            if profile is None:
                profile = self._profile_factories[profile_id](session_id)
        except KeyError:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="invalid_intent"
            )
        return await self._launch_profile(session_id, project_id, profile_id, profile)

    async def resume(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        source_id: ProviderConversationId,
    ) -> TerminalObservation:
        """Launch only a curated, adapter-resolved resume profile on the owned tmux server."""
        try:
            profile = self._resume_profile_factories[profile_id](session_id, source_id)
        except KeyError:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="invalid_intent"
            )
        return await self._launch_profile(session_id, project_id, profile_id, profile)

    async def _launch_profile(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        profile: LaunchProfile,
    ) -> TerminalObservation:
        """Persist and execute one already-curated profile through the fixed tmux runner."""
        try:
            cwd = self._project_paths[project_id].resolve(strict=True)
        except (KeyError, OSError):
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="invalid_intent"
            )
        intent_directory = self._gateway.intent_directory
        if open_private_directory(intent_directory) is None:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="invalid_intent"
            )
        document = {
            "session_id": str(session_id),
            "profile_id": str(profile_id),
            "executable": profile.executable,
            "argv": list(profile.argv),
            "cwd": str(cwd),
            "environment": profile.environment,
        }
        if self.invalidate_next_intent:
            document["session_id"] = str(SessionId.new())
            self.invalidate_next_intent = False
        path = intent_directory / f"{session_id}.json"
        # The mode belongs to the open, so a *new* file is never briefly world-readable. This
        # document carries the launch environment and argv, which is exactly what must not be
        # read in that window. O_TRUNC rather than O_EXCL, because relaunching one session
        # rewrites its intent.
        # Refusing anywhere below is the same answer the directory guard above gives, for the
        # same class of failure. O_NOFOLLOW exists here to refuse a link planted at this exact
        # name, and refusing by raising would have gone uncaught all the way out through the
        # Telegram handler, leaving the record STARTING for reconciliation to find. A launch
        # that cannot write its intent has not launched.
        refused = TerminalObservation(
            session_id, live=False, preserved=False, detail="invalid_intent"
        )
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError:
            return refused
        try:
            # Not redundant with that mode: open applies it only when it creates the file, so
            # an intent left behind at a looser mode by an older build would keep it forever.
            # Before the write, not after, because the window being closed is precisely the
            # one where the document is on disk -- repairing the mode afterwards left the
            # launch environment and argv readable for exactly as long as the write took. On
            # the descriptor rather than the path, so the name is not resolved a second time:
            # O_NOFOLLOW has already decided what this frame is writing to.
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except OSError:
            # Only reachable while the descriptor is still this frame's to close. Once
            # `fdopen` returns, the file object owns it and the `with` below is what closes
            # it -- closing here as well would be a double close. Splitting the steps is the
            # whole point: one `try` around all of them leaked the descriptor on every failed
            # launch, and this service runs for weeks.
            os.close(descriptor)
            return refused
        try:
            with handle:
                handle.write(json.dumps(document))
        except OSError:
            return refused
        try:
            await self._gateway.launch(session_id, project_id, profile_id, cwd)
        except RuntimeError:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="launch_failed"
            )
        self._session_profiles[session_id] = profile
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            observation = await self.inspect(session_id)
            capture = await self._gateway.capture(session_id) if observation is not None else ""
            if (
                observation is not None
                and observation.live
                and (profile.readiness_marker is None or profile.readiness_marker in capture)
                and not any(blocker in capture for blocker in profile.readiness_blockers)
            ):
                return observation
            await asyncio.sleep(0.01)
        return TerminalObservation(
            session_id, live=False, preserved=False, detail="startup_timeout"
        )

    def _resolved_profile(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> LaunchProfile | None:
        """Resolve a profile for a session this process may not have launched itself.

        The remembered profile is process-local, so a session started by the other
        surface — or by this one before a restart — has to be resolved from the curated
        factories, or it could never be stopped by anything but a force.
        """
        remembered = self._session_profiles.get(session_id) or self._profiles.get(profile_id)
        if remembered is not None:
            return remembered
        try:
            return self._profile_factories[profile_id](session_id)
        except KeyError:
            return None

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Send a known profile sequence only after rechecking current trusted ownership.

        **A pane that is not live is a stop that was never sent** (DEC-022), and saying so is
        the whole reason this checks liveness before typing rather than after. tmux answers
        `send-keys` at a dead pane with exit 0 and no effect (Claim 10), so an unchecked stop
        into a pane that had already died out of band — an OOM kill, a crash, anything between
        the last reconciliation pass and the owner pressing Stop — would find `preserved` true
        on its very first poll, because it was true before any key was sent, and report a
        graceful exit this service did not cause. The record then reads
        GRACEFUL_STOP_REQUESTED → PANE_EXITED → CLEANUP_CONFIRMED: a history asserting a
        sequence that never left the host.
        """
        profile = self._resolved_profile(session_id, profile_id)
        observation = await self.inspect(session_id)
        if profile is None or observation is None or observation.profile_id != profile_id:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail=UNKNOWN_SESSION
            )
        if not observation.live:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail=UNKNOWN_SESSION
            )
        try:
            await self._gateway.send_keys(session_id, profile.graceful_keys)
        except TerminalTargetMissing:
            # The pane went while the sequence was in flight. Reported as never-sent, which
            # *understates* — a key may well have landed. **DEC-038 accepted cost 2** records
            # this, because it is the case DEC-022 did not enumerate and a code comment is not
            # where an accepted inaccuracy in the durable history belongs. Understating is the
            # side to err on: the alternative claims a graceful exit this service can no
            # longer show it caused. Before this, the typed error escaped the use case
            # entirely, after GRACEFUL_STOP_REQUESTED was already written, and the record stuck at
            # STOP_REQUESTED behind a generic "stop failed" — the one outcome DEC-022 exists
            # to replace with an event that names its cause.
            return TerminalObservation(
                session_id, live=False, preserved=False, detail=UNKNOWN_SESSION
            )
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            observation = await self.inspect(session_id)
            if observation is not None and observation.preserved:
                return observation
            await asyncio.sleep(0.01)
        return TerminalObservation(session_id, live=True, preserved=False, detail=GRACEFUL_TIMEOUT)

    async def confirm_ready(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Recheck a failed launch against the profile's readiness evidence."""
        profile = self._resolved_profile(session_id, profile_id)
        if profile is None:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="unknown_profile"
            )
        observation = await self.inspect(session_id)
        if observation is None or not observation.live:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="terminal_not_live"
            )
        capture = await self._gateway.capture(session_id)
        if (
            profile.readiness_marker is not None and profile.readiness_marker not in capture
        ) or any(blocker in capture for blocker in profile.readiness_blockers):
            return TerminalObservation(session_id, live=False, preserved=False, detail="not_ready")
        return observation

    async def cleanup(self, session_id: SessionId) -> None:
        """Remove only the exact managed session after preserved-output inspection.

        A pane that is already gone leaves nothing to kill but still leaves this process
        holding its profile and its intent file, so the removal is treated as done rather
        than raised. Cleaning up after a session the terminal destroyed on its own is the
        case that most needs to succeed.
        """
        try:
            await self._gateway.destroy(session_id)
        except TerminalTargetMissing:
            pass
        self._session_profiles.pop(session_id, None)
        (self._gateway.intent_directory / f"{session_id}.json").unlink(missing_ok=True)

    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        """Recheck present trusted ownership immediately before exact target removal."""
        inventory = await self._gateway.inventory()
        if not any(pane.session_id == session_id for pane in inventory.managed):
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="ownership_lost"
            )
        await self._gateway.destroy(session_id)
        self._session_profiles.pop(session_id, None)
        (self._gateway.intent_directory / f"{session_id}.json").unlink(missing_ok=True)
        return TerminalObservation(session_id, live=False, preserved=False)

    async def inspect(self, session_id: SessionId) -> TerminalObservation | None:
        """Convert trusted dedicated-server pane evidence into terminal liveness."""
        try:
            inventory = await self._gateway.inventory()
        except RuntimeError:
            return None
        for pane in inventory.managed:
            if pane.session_id == session_id:
                return TerminalObservation(
                    session_id,
                    pane.live,
                    pane.preserved,
                    project_id=pane.project_id,
                    profile_id=pane.profile_id,
                    # Answered here as fully as `managed_observations` answers it, from the
                    # same decoded pane. `None` on this field is the port's way of saying a
                    # terminal cannot track hosting at all; one adapter filling it on one path
                    # and leaving it empty on another would make the same value mean two
                    # things, and a caller could not tell which.
                    host_session=pane.session_name,
                )
        return None

    async def capture(self, session_id: SessionId) -> str:
        """Return one managed pane's output for the presentation boundary to sanitize."""
        return await self._gateway.capture(session_id)

    async def pane_title(self, session_id: SessionId) -> str:
        """Return tmux metadata for one managed pane, never its captured output."""
        return await self._gateway.pane_title(session_id)

    async def copy_attach(self, session_id: SessionId) -> str | None:
        """Recheck the exact trusted pane immediately before rendering its attach command.

        Two panes qualify now, and they get different commands (DEC-021). A live pane attaches
        writably, as it always has. A **preserved** pane — the agent exited and tmux kept its
        output — attaches read-only: tmux will allow it, the output is the thing PRESERVED
        exists to keep, and the previous refusal read as though tmux forbade it.

        The recheck itself is unchanged and still the point: this answers from a fresh
        observation rather than from the record, so a pane that has gone since the row was
        drawn still yields nothing — **and it is what makes the host trustworthy**. The pane
        moves; an attach command built from anything older than the observation that produced
        it would name where the agent used to be shown.

        **The command names the session showing the pane**, which is the console while this
        agent is displayed there and its own session otherwise. Attach is the one
        agent-reaching operation that cannot name a pane — a tmux client attaches to a
        session — so this is what "follow the agent" means for it (DEC-021, re-scoped).

        `host_session` is a property of the *listing*, not of the pane, and the difference
        cost a real defect: tmux lists a linked window's pane under every session linked to
        it, in alphabetical order, so `inventory`'s dedup was choosing the host by whether a
        session's random id sorted before or after "console". A session that had never moved
        got `ra-console:`. `inventory` now keeps the home listing whenever one exists, so a
        pane is reported as hosted elsewhere only when nothing lists it under its own name —
        which is what displaced actually means.
        """
        observation = await self.inspect(session_id)
        if observation is None or not (observation.live or observation.preserved):
            return None
        return attach_command(
            session_id, read_only=not observation.live, host=observation.host_session
        )

    async def remote_control(
        self, session_id: SessionId, desired_state: RemoteControlState
    ) -> RemoteControlState:
        """Run only the qualified Claude key sequences against one idle exact managed pane."""
        observation = await self.inspect(session_id)
        if (
            observation is None
            or not observation.live
            or observation.profile_id != ProfileId("claude")
        ):
            return RemoteControlState.UNKNOWN
        current = _remote_control_state(await self._gateway.capture(session_id))
        if current is desired_state:
            return current
        if desired_state is RemoteControlState.INACTIVE and current is RemoteControlState.UNKNOWN:
            return current
        if desired_state is RemoteControlState.ACTIVE:
            await self._gateway.send_keys(session_id, REMOTE_CONTROL_ENABLE_KEYS)
            await asyncio.sleep(_REMOTE_CONTROL_ENABLE_WAIT_SECONDS)
        else:
            await self._gateway.send_keys(session_id, REMOTE_CONTROL_OPEN_MENU_KEYS)
            await asyncio.sleep(_REMOTE_CONTROL_MENU_WAIT_SECONDS)
            await self._gateway.send_keys(session_id, REMOTE_CONTROL_DISCONNECT_KEYS)
            await asyncio.sleep(_REMOTE_CONTROL_DISABLE_WAIT_SECONDS)
        return _remote_control_state(await self._gateway.capture(session_id))

    async def trust_state(self, session_id: SessionId) -> TrustState:
        """Report whether this pane is sitting on the folder-trust question.

        Read-only, and deliberately answerable for a pane whose *record* is FAILED: that is
        precisely the state a trust-blocked launch lands in, because the readiness marker
        never arrives and the startup budget expires. Requiring a live-and-RUNNING session
        here would make the one state this exists to rescue the one state it refuses.
        """
        observation = await self.inspect(session_id)
        if (
            observation is None
            or not observation.live
            or observation.profile_id not in TRUST_ANSWERABLE
        ):
            return TrustState.UNKNOWN
        return classify_trust_capture(await self._gateway.capture(session_id))

    async def answer_trust(self, session_id: SessionId) -> TrustState:
        """Answer the folder-trust question, and only when it is actually on screen.

        The guard is the whole safety story. `TRUST_KEYS` is a bare Enter, which is
        meaningful to every agent that ever runs in a pane -- so sending it to a session
        that is *not* asking this question is sending a stray keypress into somebody's
        work. Re-reading the pane here, rather than trusting the caller's earlier read,
        closes the window between a surface rendering the button and the owner pressing it.
        """
        if await self.trust_state(session_id) is not TrustState.AWAITING:
            return TrustState.UNKNOWN
        await self._gateway.send_keys(session_id, TRUST_KEYS)
        await asyncio.sleep(_TRUST_ANSWER_WAIT_SECONDS)
        return classify_trust_capture(await self._gateway.capture(session_id))

    async def managed_observations(self) -> tuple[TerminalObservation, ...]:
        """Return trusted dedicated-server evidence for read-only reconciliation.

        A failed query is raised, never reported as an empty server. Reconciliation reads
        an empty result as proof that every recorded session is gone, so swallowing the
        failure here would end every live session's record on one unlucky tmux call. An
        absent server is not a failure: it is the one way to observe zero managed panes.
        """
        inventory = await self._gateway.inventory()
        return tuple(
            TerminalObservation(
                pane.session_id,
                pane.live,
                pane.preserved,
                project_id=pane.project_id,
                profile_id=pane.profile_id,
                host_session=pane.session_name,
            )
            for pane in inventory.managed
        )

    async def managed_process_roots(self) -> tuple[int, ...]:
        """Expose trusted dedicated-pane roots solely for external-process exclusion."""
        try:
            inventory = await self._gateway.inventory()
        except RuntimeError:
            return ()
        return tuple(pane.process_id for pane in inventory.managed)


def _remote_control_state(capture: str) -> RemoteControlState:
    return RemoteControlState(classify_remote_control_capture(capture).value)
