"""Bounded read-only Codex thread/list catalogue through its app-server protocol."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from remote_agents.adapters.agents.protocols import JsonRpcProcess, ProtocolError
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
    display_description,
)
from remote_agents.domain.models import ProfileId, ProjectId

_PAGE_LIMIT = 50
_MAX_CATALOGUE_ITEMS = 250


class CodexThreadClient(Protocol):
    async def list_threads(
        self, *, cwd: Path, cursor: str | None, limit: int
    ) -> Mapping[str, object]: ...


class JsonRpcClient(Protocol):
    async def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]: ...


class CodexAppServerClient:
    """Map the reviewed `thread/list` request without exposing JSON-RPC to callers."""

    def __init__(self, protocol: JsonRpcClient | None = None) -> None:
        self._protocol = protocol or JsonRpcProcess(("codex", "app-server"))

    async def list_threads(
        self, *, cwd: Path, cursor: str | None, limit: int
    ) -> Mapping[str, object]:
        if limit < 1 or limit > _PAGE_LIMIT:
            raise ValueError("Codex thread/list limit is outside the reviewed bound")
        params: dict[str, object] = {"cwd": str(cwd), "limit": limit, "useStateDbOnly": True}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._protocol.request("thread/list", params)

    async def aclose(self) -> None:
        """Close the app-server session this client opened, if it opened one.

        Added alongside the Remote Control adapter's own `aclose`, which is where the leak
        was first noticed: both hold a `JsonRpcProcess` that spawns a `codex` child on first
        request, and neither had any way for a caller to reclaim it. Tolerant of an injected
        protocol with no `close`, so a test double needs no lifecycle.
        """
        close = getattr(self._protocol, "close", None)
        if close is not None:
            await close()


class CodexSessionCatalogue:
    """Collect at most a fixed number of content-free local Codex thread records."""

    def __init__(self, project_paths: Mapping[ProjectId, Path], client: CodexThreadClient) -> None:
        self._project_paths = project_paths
        self._client = client
        self._resolved: dict[ConversationReference, ResolvedConversation] = {}

    async def list_conversations(
        self,
        *,
        profile_id: ProfileId | None,
        project_id: ProjectId | None,
        page: int,
        page_size: int,
    ) -> ConversationCataloguePage:
        if profile_id is not None and profile_id != ProfileId("codex"):
            return ConversationCataloguePage((), page, 1, "profile_not_supported")
        if page < 1 or page_size < 1:
            raise ValueError("catalogue page bounds are invalid")
        projects = self._projects_for(project_id)
        if projects is None:
            return ConversationCataloguePage((), page, 1, "project_not_available")
        try:
            entries = await self._collect(projects)
        except (OSError, ProtocolError):
            return ConversationCataloguePage((), page, 1, "catalogue_unavailable")
        entries.sort(key=lambda entry: entry.summary.updated_at, reverse=True)
        self._resolved = {entry.summary.reference: entry for entry in entries}
        return _page(entries, page, page_size)

    async def resolve_conversation(
        self, reference: ConversationReference
    ) -> ResolvedConversation | None:
        return self._resolved.get(reference)

    async def resume_capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("codex"), True, True),)

    async def _collect(
        self, projects: tuple[tuple[ProjectId, Path], ...]
    ) -> list[ResolvedConversation]:
        entries: list[ResolvedConversation] = []
        for project_id, cwd in projects:
            cursor: str | None = None
            while len(entries) < _MAX_CATALOGUE_ITEMS:
                payload = await self._client.list_threads(cwd=cwd, cursor=cursor, limit=_PAGE_LIMIT)
                threads = payload.get("data")
                if not isinstance(threads, list):
                    raise ProtocolError("Codex thread/list returned no thread list")
                entries.extend(_thread_entries(threads, project_id))
                next_cursor = payload.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        return entries[:_MAX_CATALOGUE_ITEMS]

    def _projects_for(
        self, project_id: ProjectId | None
    ) -> tuple[tuple[ProjectId, Path], ...] | None:
        if project_id is None:
            return tuple(self._project_paths.items())
        path = self._project_paths.get(project_id)
        return None if path is None else ((project_id, path),)


def _thread_entries(rows: list[object], project_id: ProjectId) -> list[ResolvedConversation]:
    entries: list[ResolvedConversation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_value = row.get("id")
        updated_at = _timestamp(row.get("updatedAt"))
        if not isinstance(source_value, str) or updated_at is None:
            continue
        try:
            source = ProviderConversationId(source_value)
        except ValueError:
            continue
        digest = sha256(f"codex\0{project_id}\0{source.value}".encode()).hexdigest()
        reference = ConversationReference(f"c-{digest}")
        summary = ConversationSummary(
            reference,
            ProfileId("codex"),
            project_id,
            ConversationState.RESUMABLE,
            updated_at,
            display_description(row.get("name") or row.get("title") or row.get("preview")),
        )
        entries.append(ResolvedConversation(summary, source))
    return entries


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


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
