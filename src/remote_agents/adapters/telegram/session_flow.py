"""Composed read-only session browsing and verified stop dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from remote_agents.adapters.telegram.inspection import InspectionResult, inspect_capture
from remote_agents.adapters.telegram.sessions import SessionPage, render_session_page
from remote_agents.adapters.telegram.stops import StopController, StopRequest
from remote_agents.domain.models import SessionId, SessionRecord


class SessionFlow:
    def __init__(
        self,
        records: Callable[[], Awaitable[tuple[SessionRecord, ...]]],
        inspect: Callable[[SessionId], Awaitable[bytes | None]],
        stops: StopController,
        service: object,
    ) -> None:
        self._records = records
        self._inspect = inspect
        self._stops = stops
        self._service = service

    async def list(self, *, page: int, page_size: int) -> SessionPage:
        return render_session_page(await self._records(), page=page, page_size=page_size)

    async def inspect_session(self, session_id: SessionId) -> InspectionResult | None:
        captured = await self._inspect(session_id)
        return None if captured is None else inspect_capture(captured)

    async def execute_stop(self, request: StopRequest) -> bool:
        record = next(
            (item for item in await self._records() if item.session_id == request.session_id), None
        )
        return (
            False if record is None else await self._stops.execute(request, self._service, record)
        )
