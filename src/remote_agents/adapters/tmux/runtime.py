"""Concrete dedicated-socket terminal adapter with bounded startup readiness."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
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
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.ports.terminal import TerminalObservation

_REMOTE_CONTROL_ENABLE_WAIT_SECONDS = 3
_REMOTE_CONTROL_MENU_WAIT_SECONDS = 1
_REMOTE_CONTROL_DISABLE_WAIT_SECONDS = 2


class AsyncTmuxRunner(TmuxRunner):
    """Run only prevalidated tmux argument vectors without a shell."""

    async def run(self, *argv: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError("tmux command failed")
        return stdout.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    """Already-curated argv and environment for one adapter-resolved profile."""

    executable: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    readiness_marker: str
    graceful_keys: tuple[str, ...] = ("C-c",)
    readiness_blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not Path(self.executable).is_absolute()
            or not self.argv
            or self.argv[0] != self.executable
            or not self.readiness_marker
        ):
            raise ValueError("profile executable and argv must be fixed and absolute")


class TmuxTerminal:
    """Resolve typed IDs locally, then report tmux observation rather than database liveness."""

    def __init__(
        self,
        gateway: TmuxGateway,
        project_paths: dict[ProjectId, Path],
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
        intent_directory.mkdir(parents=True, exist_ok=True)
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
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, 0o600)
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
                and profile.readiness_marker in capture
                and not any(blocker in capture for blocker in profile.readiness_blockers)
            ):
                return observation
            await asyncio.sleep(0.01)
        return TerminalObservation(
            session_id, live=False, preserved=False, detail="startup_timeout"
        )

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Send a known profile sequence only after rechecking current trusted ownership."""
        profile = self._session_profiles.get(session_id) or self._profiles.get(profile_id)
        observation = await self.inspect(session_id)
        if profile is None or observation is None or observation.profile_id != profile_id:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="unknown_session"
            )
        await self._gateway.send_keys(session_id, profile.graceful_keys)
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            observation = await self.inspect(session_id)
            if observation is not None and observation.preserved:
                return observation
            await asyncio.sleep(0.01)
        return TerminalObservation(
            session_id, live=True, preserved=False, detail="graceful_timeout"
        )

    async def confirm_ready(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Recheck a failed launch against the profile's readiness evidence."""
        profile = self._session_profiles.get(session_id) or self._profiles.get(profile_id)
        if profile is None:
            try:
                profile = self._profile_factories[profile_id](session_id)
            except KeyError:
                return TerminalObservation(
                    session_id, live=False, preserved=False, detail="unknown_profile"
                )
        observation = await self.inspect(session_id)
        if observation is None or not observation.live:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="terminal_not_live"
            )
        capture = await self._gateway.capture(session_id)
        if profile.readiness_marker not in capture or any(
            blocker in capture for blocker in profile.readiness_blockers
        ):
            return TerminalObservation(session_id, live=False, preserved=False, detail="not_ready")
        return observation

    async def cleanup(self, session_id: SessionId) -> None:
        """Remove only the exact managed session after preserved-output inspection."""
        await self._gateway.mutate("kill-session", f"ra-{session_id}")
        self._session_profiles.pop(session_id, None)
        (self._gateway.intent_directory / f"{session_id}.json").unlink(missing_ok=True)

    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        """Recheck present trusted ownership immediately before exact target removal."""
        inventory = await self._gateway.inventory()
        if not any(pane.session_id == session_id for pane in inventory.managed):
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="ownership_lost"
            )
        await self._gateway.mutate("kill-session", f"ra-{session_id}")
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
                )
        return None

    async def capture(self, session_id: SessionId) -> str:
        """Return one managed pane's output for the presentation boundary to sanitize."""
        return await self._gateway.capture(session_id)

    async def copy_attach(self, session_id: SessionId) -> str | None:
        """Recheck exact trusted pane liveness immediately before rendering its attach command."""
        observation = await self.inspect(session_id)
        if observation is None or not observation.live:
            return None
        return attach_command(session_id)

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
        if (
            desired_state is RemoteControlState.INACTIVE
            and current is RemoteControlState.UNKNOWN
        ):
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

    async def managed_observations(self) -> tuple[TerminalObservation, ...]:
        """Return trusted dedicated-server evidence for read-only reconciliation."""
        try:
            inventory = await self._gateway.inventory()
        except RuntimeError:
            return ()
        return tuple(
            TerminalObservation(
                pane.session_id,
                pane.live,
                pane.preserved,
                project_id=pane.project_id,
                profile_id=pane.profile_id,
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
