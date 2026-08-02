"""Fail-closed Linux local-agent discovery using only executable, cwd, terminal, and FD links."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.external_sessions import (
    ExternalSessionReference,
    ExternalSessionState,
    ExternalSessionSummary,
    ResolvedExternalSession,
)
from remote_agents.domain.models import ProfileId, ProjectId

_MAX_PROCESSES = 1_024
_MAX_FD_LINKS = 256
_CURATED_EXECUTABLES = {
    "claude": ProfileId("claude"),
    "codex": ProfileId("codex"),
    "opencode": ProfileId("opencode"),
    "cursor-agent": ProfileId("cursor-agent"),
}


@dataclass(frozen=True, slots=True)
class _Evidence:
    pid: int
    executable: Path
    cwd: Path
    terminal: str


class LinuxLocalProcessCatalog:
    """Read metadata links only; never opens process streams or controls an external process."""

    def __init__(
        self,
        project_paths: Mapping[ProjectId, Path],
        *,
        proc_root: Path = Path("/proc"),
        claude_sessions_root: Path = Path.home() / ".claude" / "projects",
    ) -> None:
        self._project_paths = {
            project_id: path.resolve(strict=False) for project_id, path in project_paths.items()
        }
        self._proc_root = proc_root
        self._claude_sessions_root = claude_sessions_root
        self._resolved: dict[ExternalSessionReference, _Evidence] = {}
        self._external: dict[ExternalSessionReference, ResolvedExternalSession] = {}

    async def list_external_sessions(self) -> tuple[ExternalSessionSummary, ...]:
        """Return only bounded summaries after a fresh read-only local scan."""
        return await asyncio.to_thread(self._scan)

    async def resolve_external_session(
        self, reference: ExternalSessionReference
    ) -> ResolvedExternalSession | None:
        return self._external.get(reference)

    async def is_still_running(self, reference: ExternalSessionReference) -> bool:
        evidence = self._resolved.get(reference)
        return evidence is not None and await asyncio.to_thread(self._matches, evidence)

    def _scan(self) -> tuple[ExternalSessionSummary, ...]:
        resolved: dict[ExternalSessionReference, _Evidence] = {}
        summaries: list[ExternalSessionSummary] = []
        try:
            process_directories = sorted(
                (path for path in self._proc_root.iterdir() if path.name.isdecimal()),
                key=lambda path: int(path.name),
            )[:_MAX_PROCESSES]
        except OSError:
            self._resolved = {}
            self._external = {}
            return ()
        for directory in process_directories:
            evidence = self._evidence(directory)
            if evidence is None:
                continue
            profile_id = _CURATED_EXECUTABLES.get(evidence.executable.name)
            if profile_id is None:
                continue
            project_id = self._project_for(evidence.cwd)
            source = (
                self._claude_source(directory, project_id)
                if profile_id == ProfileId("claude")
                else None
            )
            state = (
                ExternalSessionState.RUNNING_EXTERNALLY
                if source is not None and evidence.terminal.startswith("/dev/")
                else ExternalSessionState.NOT_SAFELY_ADOPTABLE
            )
            reference = _reference(profile_id, evidence, source)
            summary = ExternalSessionSummary(reference, profile_id, project_id, state)
            summaries.append(summary)
            resolved[reference] = evidence
            self._external[reference] = ResolvedExternalSession(summary, evidence.pid, source)
        self._resolved = resolved
        return tuple(summaries)

    def _matches(self, expected: _Evidence) -> bool:
        actual = self._evidence(self._proc_root / str(expected.pid))
        return actual == expected

    def _evidence(self, directory: Path) -> _Evidence | None:
        try:
            return _Evidence(
                int(directory.name),
                Path(os.readlink(directory / "exe")),
                Path(os.readlink(directory / "cwd")).resolve(strict=False),
                os.readlink(directory / "fd" / "0"),
            )
        except (OSError, ValueError):
            return None

    def _project_for(self, cwd: Path) -> ProjectId | None:
        return next(
            (project_id for project_id, path in self._project_paths.items() if path == cwd), None
        )

    def _claude_source(
        self, directory: Path, project_id: ProjectId | None
    ) -> ProviderConversationId | None:
        if project_id is None:
            return None
        expected_parent = self._claude_sessions_root / _claude_project_directory(
            self._project_paths[project_id]
        )
        try:
            links = sorted(path for path in (directory / "fd").iterdir())[:_MAX_FD_LINKS]
        except OSError:
            return None
        for link in links:
            try:
                target = Path(os.readlink(link))
            except OSError:
                continue
            if target.parent != expected_parent or target.suffix != ".jsonl":
                continue
            try:
                parsed = UUID(target.stem)
            except ValueError:
                continue
            if str(parsed) == target.stem:
                return ProviderConversationId(str(parsed))
        return None


def _reference(
    profile_id: ProfileId, evidence: _Evidence, source: ProviderConversationId | None
) -> ExternalSessionReference:
    digest = sha256(
        f"{profile_id}\0{evidence.pid}\0{evidence.executable}\0{evidence.cwd}\0{source}".encode()
    ).hexdigest()
    return ExternalSessionReference(f"p-{digest}")


def _claude_project_directory(path: Path) -> str:
    return str(path.resolve(strict=False)).replace("/", "-")
