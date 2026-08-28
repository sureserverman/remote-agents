from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from remote_agents.adapters.agents.activity_spool import _observed_event


def test_codex_keeps_only_the_two_actionable_event_names() -> None:
    observed = _observed_event(
        BytesIO(
            b'{"hook_event_name":"PermissionRequest","tool_input":"secret","cwd":"/x",'
            b'"session_id":"provider-session"}'
        ),
        "session",
        datetime.now(UTC),
        "codex",
    )
    assert observed is not None
    assert observed.session_id == "session"
    assert observed.event == "PermissionRequest"
    assert observed.reason is None and observed.detail is None
    assert set(observed.document()) == {"session_id", "event", "reason", "detail", "observed_at"}


def test_codex_stop_is_spooled_without_provider_payload_fields() -> None:
    observed = _observed_event(
        BytesIO(
            b'{"hook_event_name":"Stop","command":"secret","transcript_path":"/x","permission_mode":"danger"}'
        ),
        "session",
        datetime.now(UTC),
        "codex",
    )
    assert observed is not None and observed.event == "Stop"
    assert observed.detail is None and observed.reason is None
