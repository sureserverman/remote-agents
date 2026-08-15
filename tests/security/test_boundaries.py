"""Security contracts for authorization, filesystem, capture, and exact tmux ownership."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from stop_results import a_verified_force_stop

from remote_agents.adapters.telegram.authorization import (
    AuthorizationGate,
    AuthorizationUpdate,
    ContentFreeDenialLog,
)
from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.adapters.tmux.capture import sanitize_capture
from remote_agents.adapters.tmux.codec import exact_session_target
from remote_agents.config import ConfigError
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.production import ProductionPaths


def test_unauthorized_callback_is_denied_before_the_callback_payload_is_handled() -> None:
    handled: list[str] = []
    gate = AuthorizationGate(7, 11, ContentFreeDenialLog())

    accepted = gate.dispatch(
        AuthorizationUpdate(sender_id=8, chat_id=11, chat_type="private", kind="callback"),
        lambda: handled.append("handled"),
    )

    assert not accepted
    assert handled == []


def test_symlinked_telegram_environment_file_is_rejected(tmp_path) -> None:
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories()
    target = tmp_path / "outside.env"
    target.write_text("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    os.chmod(target, 0o600)
    paths.environment_path.symlink_to(target)

    with pytest.raises(ConfigError, match="owned regular file"):
        paths.require_private_environment()


def test_pane_capture_removes_control_sequences_before_returning_it() -> None:
    assert sanitize_capture(b"\x1b[31mred\x1b[0m\x00", max_lines=2, max_bytes=100) == "red"


async def test_force_stop_rechecks_the_current_record_before_dispatch() -> None:
    session_id = SessionId.new()
    profile_id = ProfileId("claude")
    callbacks = CallbackStateStore()
    controller = StopController(callbacks)
    token = controller.offer_confirmed_force(
        session_id, profile_id, SessionState.RUNNING, None, 7, 11
    )
    assert token is not None
    callbacks.bind_pending(11, 1)
    request = controller.claim(token, 7, 11, 1)
    assert request is not None
    service = RecordingForceService()
    changed = replace(_record(session_id, profile_id), state=SessionState.ENDED)

    accepted = await controller.execute(request, service, changed)

    # `.dispatched`, never the result itself: `StopResult` refuses to be a bool precisely so
    # this line cannot quietly stop checking anything, which is what it did for one commit.
    assert not accepted.dispatched
    assert service.calls == 0


def test_tmux_target_requires_a_managed_canonical_session_id() -> None:
    with pytest.raises(ValueError, match="managed session name"):
        exact_session_target("default")


def _record(session_id: SessionId, profile_id: ProfileId) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        profile_id,
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


class RecordingForceService:
    def __init__(self) -> None:
        self.calls = 0

    async def force_stop(self, command: object):
        self.calls += 1
        del command
        return a_verified_force_stop()
