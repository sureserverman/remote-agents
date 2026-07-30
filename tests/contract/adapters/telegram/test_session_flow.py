from __future__ import annotations

import pytest

from remote_agents.adapters.telegram.session_flow import SessionFlow


@pytest.mark.asyncio
async def test_missing_session_fails_closed_before_stop_dispatch() -> None:
    class Stops:
        async def execute(self, _request, _service, _record):
            raise AssertionError("should not execute")

    class Request:
        session_id = object()

    async def records():
        return ()

    async def inspect(_session_id):
        return None

    flow = SessionFlow(records, inspect, Stops(), object())

    assert not await flow.execute_stop(Request())
