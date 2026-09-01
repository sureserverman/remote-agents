"""Claude session catalogue using UUID paths, mtimes, and bounded resume descriptions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

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


class ClaudeSessionCatalogue:
    """List UUID sessions with a bounded resume description.

    Not "owner-approved", which is what this said before BL-007: nothing approves the
    description. It is the owner's own last prompt or the provider's generated title,
    bounded and printable-checked and screened for nothing.
    """

    def __init__(
        self,
        project_paths: Mapping[ProjectId, Path],
        *,
        sessions_root: Path = Path.home() / ".claude" / "projects",
    ) -> None:
        self._project_paths = project_paths
        self._sessions_root = sessions_root
        self._resolved: dict[ConversationReference, ResolvedConversation] = {}

    async def list_conversations(
        self,
        *,
        profile_id: ProfileId | None,
        project_id: ProjectId | None,
        page: int,
        page_size: int,
    ) -> ConversationCataloguePage:
        if profile_id is not None and profile_id != ProfileId("claude"):
            return ConversationCataloguePage((), page, 1, "profile_not_supported")
        if page < 1 or page_size < 1:
            raise ValueError("catalogue page bounds are invalid")
        projects = self._projects_for(project_id)
        if projects is None:
            return ConversationCataloguePage((), page, 1, "project_not_available")
        entries: list[ResolvedConversation] = []
        try:
            for candidate_project_id, project_path in projects:
                directory = self._sessions_root / _claude_project_directory(project_path)
                if not directory.is_dir():
                    continue
                for transcript in directory.glob("*.jsonl"):
                    source = _uuid_filename(transcript)
                    if source is None:
                        continue
                    updated_at = datetime.fromtimestamp(transcript.stat().st_mtime, UTC)
                    summary = ConversationSummary(
                        _reference(candidate_project_id, source),
                        ProfileId("claude"),
                        candidate_project_id,
                        ConversationState.RESUMABLE,
                        updated_at,
                        _resume_description(transcript),
                    )
                    entries.append(ResolvedConversation(summary, source))
        except OSError:
            return ConversationCataloguePage((), page, 1, "catalogue_unavailable")
        entries.sort(key=lambda entry: entry.summary.updated_at, reverse=True)
        self._resolved = {entry.summary.reference: entry for entry in entries}
        return _page(entries, page, page_size)

    async def resolve_conversation(
        self, reference: ConversationReference
    ) -> ResolvedConversation | None:
        return self._resolved.get(reference)

    async def resume_capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        available = self._sessions_root.is_dir()
        return (
            ProfileResumeCapability(
                ProfileId("claude"),
                available,
                available,
                None if available else "catalogue_unavailable",
            ),
        )

    def _projects_for(
        self, project_id: ProjectId | None
    ) -> tuple[tuple[ProjectId, Path], ...] | None:
        if project_id is None:
            return tuple(self._project_paths.items())
        path = self._project_paths.get(project_id)
        return None if path is None else ((project_id, path),)


def _claude_project_directory(path: Path) -> str:
    return str(path.resolve(strict=False)).replace("/", "-")


def _uuid_filename(path: Path) -> ProviderConversationId | None:
    try:
        parsed = UUID(path.stem)
    except ValueError:
        return None
    return ProviderConversationId(str(parsed)) if str(parsed) == path.stem else None


def _reference(project_id: ProjectId, source: ProviderConversationId) -> ConversationReference:
    digest = sha256(f"claude\0{project_id}\0{source.value}".encode()).hexdigest()
    return ConversationReference(f"c-{digest}")


def _resume_description(transcript: Path) -> str | None:
    """Read at most 256 records for a generated title or Claude's resume description."""
    last_prompt: str | None = None
    try:
        with transcript.open(encoding="utf-8") as records:
            for _ in range(256):
                line = records.readline(16_385)
                if not line:
                    return last_prompt
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "ai-title":
                    return display_description(record.get("aiTitle"))
                if record.get("type") == "last-prompt":
                    last_prompt = display_description(record.get("lastPrompt"))
    except OSError:
        return None
    return last_prompt


def _page(
    entries: list[ResolvedConversation], page: int, page_size: int
) -> ConversationCataloguePage:
    page_count = max(1, (len(entries) + page_size - 1) // page_size)
    if page > page_count:
        raise ValueError("catalogue page is out of range")
    start = (page - 1) * page_size
    selected = entries[start : start + page_size]
    return ConversationCataloguePage(tuple(entry.summary for entry in selected), page, page_count)
