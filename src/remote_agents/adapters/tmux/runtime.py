"""Concrete dedicated-socket terminal adapter with bounded startup readiness."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from remote_agents.adapters.tmux.gateway import TmuxGateway, TmuxRunner
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.terminal import TerminalObservation


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
    ) -> None:
        self._gateway = gateway
        self._project_paths = project_paths
        self._profiles = profiles
        self._startup_timeout = startup_timeout
        self.invalidate_next_intent = False
        self._session_profiles: dict[SessionId, LaunchProfile] = {}

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Persist a resolved intent, launch it, then require observed pane liveness."""
        try:
            cwd = self._project_paths[project_id].resolve(strict=True)
            profile = self._profiles[profile_id]
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
            if (
                observation is not None
                and observation.live
                and profile.readiness_marker in await self._gateway.capture(session_id)
            ):
                return observation
            await asyncio.sleep(0.01)
        return TerminalObservation(
            session_id, live=False, preserved=False, detail="startup_timeout"
        )

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Send only the persisted profile sequence and retain the resulting dead pane."""
        profile = self._session_profiles.get(session_id)
        if profile is None or profile_id not in self._profiles:
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

    async def cleanup(self, session_id: SessionId) -> None:
        """Remove only the exact managed session after preserved-output inspection."""
        await self._gateway.mutate("kill-session", f"ra-{session_id}")
        self._session_profiles.pop(session_id, None)
        (self._gateway.intent_directory / f"{session_id}.json").unlink(missing_ok=True)

    async def inspect(self, session_id: SessionId) -> TerminalObservation | None:
        """Convert trusted dedicated-server pane evidence into terminal liveness."""
        try:
            inventory = await self._gateway.inventory()
        except RuntimeError:
            return None
        for pane in inventory.managed:
            if pane.session_id == session_id:
                return TerminalObservation(session_id, pane.live, pane.preserved)
        return None
