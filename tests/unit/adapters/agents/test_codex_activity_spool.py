from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from remote_agents.adapters.agents.activity_spool import _observed_event


def test_codex_keeps_only_the_two_actionable_event_names() -> None:
    observed = _observed_event(
        BytesIO(b'{"hook_event_name":"PermissionRequest","tool_input":"secret","cwd":"/x"}'),
        "session",
        datetime.now(UTC),
        "codex",
    )
    assert observed is not None
    assert observed.event == "PermissionRequest"
    assert observed.reason is None and observed.detail is None
