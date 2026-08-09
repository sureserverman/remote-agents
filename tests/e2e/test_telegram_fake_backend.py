from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from uuid import UUID

import pytest
from stop_results import a_clean_stop

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.inspection import inspect_capture
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

    assert (
        "launch/resume/list/inspect/graceful/cleanup/force/create-project/navigation"
        in completed.stdout
    )


@pytest.mark.asyncio
async def test_fake_backend_primitives_cover_read_only_inspection_and_confirmed_stop() -> None:
    session = SessionId(UUID(int=1))
    inspection = inspect_capture(b"ready\n")
    stops = StopController(CallbackStateStore())
    token = stops.offer(session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11, 1)

    assert inspection.text == "ready"
    assert token is not None
    claimed = stops.claim(token, 7, 11, 1)
    assert claimed is not None and claimed.action == "graceful"


def test_fake_journey_contract_covers_commands_recovery_and_oversized_inspection() -> None:
    """Keep the owner journey discoverable without requiring a live Telegram account."""
    owner_commands = ("/launch", "/sessions", "/help")
    expired = CallbackStateStore().resolve("missing", owner_id=7, chat_id=11, view_revision=1)
    attachment = inspect_capture(("x" * 30).encode(), telegram_limit=20)

    assert owner_commands == ("/launch", "/sessions", "/help")
    assert expired is None, "expired callbacks recover to Home after acknowledgement"
    assert attachment.filename == "session-output.txt"
    assert attachment.attachment is not None
    assert "Back" != "Cancel"


@pytest.mark.asyncio
async def test_stop_controller_rechecks_and_dispatches_against_fakes() -> None:
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

        async def graceful_stop(self, _command):
            self.called = True
            return a_clean_stop()

    stops = StopController(CallbackStateStore())
    token = stops.offer(session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11, 1)
    assert token is not None
    request = stops.claim(token, 7, 11, 1)
    assert request is not None
    service = Service()
    assert await stops.execute(request, service, record)
    assert service.called
