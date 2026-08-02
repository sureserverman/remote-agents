"""Bounded OpenCode JSON catalogue adapter; titles and transcript text are discarded."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from remote_agents.adapters.agents.protocols import ProtocolError
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId

_MAX_CATALOGUE_ITEMS = 250


class OpenCodeSessionRunner(Protocol):
    async def list_sessions(self, limit: int) -> str: ...


class OpenCodeCliRunner:
    """Run the one reviewed machine-readable OpenCode catalogue argv."""

    async def list_sessions(self, limit: int) -> str:
        if limit < 1 or limit > _MAX_CATALOGUE_ITEMS:
            raise ValueError("OpenCode catalogue limit is outside the reviewed bound")
        process = await asyncio.create_subprocess_exec(
            "opencode",
            "session",
            "list",
            "--format",
            "json",
            "--max-count",
            str(limit),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        if process.returncode:
            raise ProtocolError("OpenCode session catalogue command failed")
        return stdout.decode("utf-8", errors="replace")


class OpenCodeSessionCatalogue:
    """Expose only project-bound opaque references from OpenCode's JSON list."""

    def __init__(
        self, project_paths: Mapping[ProjectId, Path], runner: OpenCodeSessionRunner
    ) -> None:
        self._project_paths = {
            project_id: path.resolve(strict=False) for project_id, path in project_paths.items()
        }
        self._runner = runner
        self._resolved: dict[ConversationReference, ResolvedConversation] = {}

    async def list_conversations(
        self,
        *,
        profile_id: ProfileId | None,
        project_id: ProjectId | None,
        page: int,
        page_size: int,
    ) -> ConversationCataloguePage:
        if profile_id is not None and profile_id != ProfileId("opencode"):
            return ConversationCataloguePage((), page, 1, "profile_not_supported")
        if page < 1 or page_size < 1:
            raise ValueError("catalogue page bounds are invalid")
        try:
            payload = json.loads(await self._runner.list_sessions(_MAX_CATALOGUE_ITEMS))
            entries = _entries(payload, self._project_paths, project_id)
        except (OSError, ProtocolError, ValueError, json.JSONDecodeError):
            return ConversationCataloguePage((), page, 1, "catalogue_unavailable")
        entries.sort(key=lambda entry: entry.summary.updated_at, reverse=True)
        self._resolved = {entry.summary.reference: entry for entry in entries}
        return _page(entries, page, page_size)

    async def resolve_conversation(
        self, reference: ConversationReference
    ) -> ResolvedConversation | None:
        return self._resolved.get(reference)

    async def resume_capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("opencode"), True, True),)


def _entries(
    payload: object, project_paths: Mapping[ProjectId, Path], project_id: ProjectId | None
) -> list[ResolvedConversation]:
    rows = payload.get("sessions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("OpenCode session list did not return an array")
    entries: list[ResolvedConversation] = []
    for row in rows[:_MAX_CATALOGUE_ITEMS]:
        if not isinstance(row, dict):
            continue
        mapped_project = _project_for(row.get("directory"), project_paths)
        if mapped_project is None or (project_id is not None and mapped_project != project_id):
            continue
        source_value = row.get("id")
        updated_at = _timestamp(row.get("updatedAt"))
        if not isinstance(source_value, str) or updated_at is None:
            continue
        try:
            source = ProviderConversationId(source_value)
        except ValueError:
            continue
        digest = sha256(f"opencode\0{mapped_project}\0{source.value}".encode()).hexdigest()
        summary = ConversationSummary(
            ConversationReference(f"c-{digest}"),
            ProfileId("opencode"),
            mapped_project,
            ConversationState.RESUMABLE,
            updated_at,
        )
        entries.append(ResolvedConversation(summary, source))
    return entries


def _project_for(value: object, project_paths: Mapping[ProjectId, Path]) -> ProjectId | None:
    if not isinstance(value, str):
        return None
    candidate = Path(value).resolve(strict=False)
    return next(
        (project_id for project_id, path in project_paths.items() if path == candidate), None
    )


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _page(
    entries: list[ResolvedConversation], page: int, page_size: int
) -> ConversationCataloguePage:
    page_count = max(1, (len(entries) + page_size - 1) // page_size)
    if page > page_count:
        raise ValueError("catalogue page is out of range")
    start = (page - 1) * page_size
    return ConversationCataloguePage(
        tuple(entry.summary for entry in entries[start : start + page_size]), page, page_count
    )
