from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from uuid import UUID

import pytest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.telegram.session_flow import SessionFlow
from remote_agents.adapters.telegram.sessions import render_session_page
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def test_telegram_action_audit_accepts_the_closed_adapter_surface() -> None:
    completed = subprocess.run(
        [sys.executable, "tests/architecture/check_telegram_actions.py"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "launch/list/inspect/graceful/cleanup/force/navigation" in completed.stdout


@pytest.mark.asyncio
async def test_fake_backend_primitives_cover_read_only_inspection_and_confirmed_stop() -> None:
    session = SessionId(UUID(int=1))
    record = SessionRecord(
        session,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "Claude", "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 7, 31, tzinfo=UTC),
    )
    page = render_session_page((record,), page=0, page_size=20, now=record.created_at)
    inspection = inspect_capture(b"ready\n")
    stops = StopController(CallbackStateStore())
    token = stops.offer(session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11, 1)

    assert page.items[0].identity.endswith("#1")
    assert inspection.text == "ready"
    assert token is not None
    claimed = stops.claim(token, 7, 11, 1)
    assert claimed is not None and claimed.action == "graceful"


@pytest.mark.asyncio
async def test_session_flow_drives_list_inspect_and_rechecked_stop_against_fakes() -> None:
    session = SessionId(UUID(int=2))
    record = SessionRecord(
        session,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "Claude", "regular", 2),
        SessionState.RUNNING,
        datetime(2026, 7, 31, tzinfo=UTC),
    )

    class Service:
        def __init__(self) -> None:
            self.called = False

        async def graceful_stop(self, _command) -> None:
            self.called = True

    async def records():
        return (record,)

    async def capture(_session_id):
        return b"safe output\n"

    stops = StopController(CallbackStateStore())
    token = stops.offer(session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11, 1)
    assert token is not None
    request = stops.claim(token, 7, 11, 1)
    assert request is not None
    service = Service()
    flow = SessionFlow(records, capture, stops, service)

    assert (await flow.list(page=0, page_size=20)).items[0].identity.endswith("#2")
    assert (await flow.inspect_session(session)).text == "safe output"
    assert await flow.execute_stop(request)
    assert service.called
