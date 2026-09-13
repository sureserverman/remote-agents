"""Codex's account limits, asked of its own app server rather than read off a rollout.

The fixture is a recorded `account/rateLimits/read` answer from codex-cli 0.154.0 with its
account id replaced (GDEC-SEC-001), so every field asserted here — the `codex` bucket under
`rateLimitsByLimitId`, `usedPercent`, `windowDurationMins`, `resetsAt` — was read out of a
live response first (DEC-013 clause 4). Nothing below `tests/live` spawns a real `codex`: the
JSON-RPC client is injected and the fake records every call.

`now` is pinned in every test. The recorded reset instants fall within days of the capture,
and the lapsed-window rule under test would otherwise drop the very windows the assertions
want the morning after — the same lesson `test_usage.py` records for its own fixtures.
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.codex.account_limits import CodexAccountLimitsReader
from remote_agents.adapters.agents.codex.usage import CodexUsageReader
from remote_agents.adapters.agents.protocols import JsonRpcProcess, ProtocolError
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    LimitsAbsence,
    UsageQuery,
    UsageWindow,
)

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "provider_contract"
    / "fixtures"
    / "codex"
    / "account_rate_limits"
    / "read-plus-0.154.0.json"
)

#: Before the fixture's earliest reset (the 5h window resets 2026-09-14 00:02:01Z).
NOW = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)

#: After the 5h window has reset and before the week's has (2026-09-19 11:48:18Z).
AFTER_FIVE_HOUR_RESET = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

FIVE_HOUR = UsageWindow("5h", 100.0, datetime.fromtimestamp(1789344121, UTC))
WEEK = UsageWindow("week", 78.0, datetime.fromtimestamp(1789818498, UTC))


def recorded() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@dataclass
class FakeClient:
    """Answers every request with one scripted result, recording each call and its close."""

    response: dict
    calls: list[tuple[str, dict]] = field(default_factory=list)
    closed: int = 0

    async def request(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        return self.response

    async def close(self) -> None:
        self.closed += 1


def reader(response: dict, *, now: datetime = NOW) -> CodexAccountLimitsReader:
    return CodexAccountLimitsReader(client=FakeClient(response), now=lambda: now)


def test_the_fixture_names_its_source_and_its_synthetic_account() -> None:
    """The evidence must say where it came from and that the identifying value is not real."""
    document = recorded()
    provenance = document["_provenance"]
    assert "account/rateLimits/read" in provenance
    assert "0.154.0" in provenance
    assert "excludeResetCreditDetails" in provenance
    assert "synthetic" in provenance
    assert document["accountId"].startswith("account-not-ours")


async def test_both_windows_come_from_the_codex_bucket_dated_now() -> None:
    limits = await reader(recorded()).limits_async()
    assert limits.profile_id == ProfileId("codex")
    assert limits.windows == (FIVE_HOUR, WEEK)
    assert limits.absence is None
    assert limits.observed_at == NOW
    assert limits.stale_source is None


async def test_the_reader_is_filed_under_codex() -> None:
    assert CodexAccountLimitsReader.profiles == frozenset({ProfileId("codex")})
    assert CodexAccountLimitsReader.limits_profile == ProfileId("codex")


async def test_a_null_primary_yields_the_one_window_that_is_stated() -> None:
    document = recorded()
    document["rateLimitsByLimitId"]["codex"]["primary"] = None
    limits = await reader(document).limits_async()
    assert limits.windows == (WEEK,)
    assert limits.absence is None


async def test_without_a_codex_bucket_the_top_level_rate_limits_answer() -> None:
    document = recorded()
    del document["rateLimitsByLimitId"]["codex"]
    limits = await reader(document).limits_async()
    assert limits.windows == (FIVE_HOUR, WEEK)


async def test_the_codex_bucket_is_preferred_over_the_top_level_figures() -> None:
    """When both are present the bucket wins, so a stale top-level copy is never the answer."""
    document = recorded()
    document["rateLimits"]["primary"]["usedPercent"] = 1
    limits = await reader(document).limits_async()
    assert limits.windows == (FIVE_HOUR, WEEK)


async def test_a_bucket_that_is_not_a_dict_falls_through_to_the_top_level() -> None:
    document = recorded()
    document["rateLimitsByLimitId"]["codex"] = "unexpected"
    limits = await reader(document).limits_async()
    assert limits.windows == (FIVE_HOUR, WEEK)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({}, id="empty-response"),
        pytest.param({"rateLimits": None, "rateLimitsByLimitId": {}}, id="both-null"),
        pytest.param(
            {"rateLimitsByLimitId": {"codex": {"primary": None, "secondary": None}}},
            id="bucket-with-no-windows",
        ),
    ],
)
async def test_no_window_anywhere_is_no_reading_never_an_invented_window(document: dict) -> None:
    limits = await reader(copy.deepcopy(document)).limits_async()
    assert limits.profile_id == ProfileId("codex")
    assert limits.windows == ()
    assert limits.absence is LimitsAbsence.NO_READING
    assert limits.stale_source is None


async def test_a_window_whose_reset_has_passed_is_dropped() -> None:
    limits = await reader(recorded(), now=AFTER_FIVE_HOUR_RESET).limits_async()
    assert limits.windows == (WEEK,)
    assert limits.observed_at == AFTER_FIVE_HOUR_RESET


async def test_the_client_is_asked_exactly_the_reviewed_request() -> None:
    client = FakeClient(recorded())
    await CodexAccountLimitsReader(client=client, now=lambda: NOW).limits_async()
    assert client.calls == [("account/rateLimits/read", {"excludeResetCreditDetails": True})]


async def test_aclose_closes_the_client() -> None:
    client = FakeClient(recorded())
    await CodexAccountLimitsReader(client=client, now=lambda: NOW).aclose()
    assert client.closed == 1


# --- Task 1.3: the rollout-file fallback, the restart, and the close ---------------------


ROLLOUT_STAMP = "rollout file"


@dataclass
class FaultingClient:
    """Raises the scripted fault on the first `n_faults` requests, then answers as `FakeClient`."""

    response: dict
    fault: BaseException
    n_faults: int = 1
    calls: int = 0
    closed: int = 0

    async def request(self, method: str, params: dict) -> dict:
        self.calls += 1
        if self.calls <= self.n_faults:
            raise self.fault
        return self.response

    async def close(self) -> None:
        self.closed += 1


@dataclass
class FakeRollout:
    """Stands in for `CodexUsageReader.limits()`: answers one recorded reading, sync."""

    reading: AgentLimits
    calls: int = 0
    queries: list[UsageQuery] = field(default_factory=list)

    def limits(self) -> AgentLimits:
        self.calls += 1
        return self.reading

    def read(self, query: UsageQuery) -> AgentUsage | None:
        self.queries.append(query)
        return AgentUsage(observed_at=FILE_MOMENT)


FILE_MOMENT = datetime(2026, 9, 13, 18, 30, tzinfo=UTC)
FILE_READING = AgentLimits(
    ProfileId("codex"), (UsageWindow("5h", 61.0, FIVE_HOUR.resets_at),), observed_at=FILE_MOMENT
)


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(ProtocolError("provider protocol response timed out"), id="protocol"),
        pytest.param(TimeoutError(), id="timeout"),
        pytest.param(FileNotFoundError(2, "No such file", "codex"), id="no-binary"),
        pytest.param(OSError(13, "Permission denied"), id="oserror"),
    ],
)
async def test_each_transport_fault_answers_with_the_rollout_file_fallback(
    fault: BaseException,
) -> None:
    """The file's reading, its own `observed_at`, and the stamp that says where it came from."""
    rollout = FakeRollout(FILE_READING)
    reader = CodexAccountLimitsReader(
        client=FaultingClient(recorded(), fault), fallback=rollout, now=lambda: NOW
    )
    limits = await reader.limits_async()
    assert limits.windows == FILE_READING.windows
    assert limits.observed_at == FILE_MOMENT
    assert limits.stale_source == ROLLOUT_STAMP
    assert limits.absence is None
    assert rollout.calls == 1


async def test_a_fallback_with_nothing_is_no_reading_still_stamped_as_the_fallback(
    tmp_path: Path,
) -> None:
    """The real rollout reader over an empty root: no invented window, and the stamp survives."""
    reader = CodexAccountLimitsReader(
        client=FaultingClient(recorded(), ProtocolError("closed")),
        fallback=CodexUsageReader(sessions_root=tmp_path, now=lambda: NOW),
        now=lambda: NOW,
    )
    limits = await reader.limits_async()
    assert limits.windows == ()
    assert limits.absence is LimitsAbsence.NO_READING
    assert limits.stale_source == ROLLOUT_STAMP


async def test_after_a_fallback_the_next_read_asks_the_rpc_again() -> None:
    """A failure discards the child rather than the transport: the next read tries the RPC."""
    client = FaultingClient(recorded(), ProtocolError("timed out"), n_faults=1)
    reader = CodexAccountLimitsReader(
        client=client, fallback=FakeRollout(FILE_READING), now=lambda: NOW
    )
    first = await reader.limits_async()
    second = await reader.limits_async()
    assert first.stale_source == ROLLOUT_STAMP
    assert second.stale_source is None
    assert second.windows == (FIVE_HOUR, WEEK)
    assert client.calls == 2
    assert client.closed == 1, "the wedged child is closed so the next read spawns a fresh one"


def _stub_app_server(tmp_path: Path, *, answers_before_exit: int) -> tuple[str, ...]:
    """A child speaking just enough JSON-RPC for this reader, then dying mid-request.

    `initialize` is answered like any other request (an empty object), so the count includes
    it: `answers_before_exit=2` answers `initialize` and one `account/rateLimits/read`, and
    exits on the *next* request without answering it -- a child that goes away with a request
    pending, which is what the client reports as "closed before responding". A child that
    exits between reads is not this case: `JsonRpcProcess` respawns one whose exit it has
    already seen, and no fault ever reaches the reader.
    """
    script = tmp_path / "stub_app_server.py"
    script.write_text(
        "import json, os, sys\n"
        f"fixture = json.load(open({str(FIXTURE)!r}))\n"
        f"budget = {answers_before_exit}\n"
        "answered = 0\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if 'id' not in message:\n"
        "        continue\n"
        "    if answered >= budget:\n"
        "        break\n"
        "    result = fixture if message['method'] == 'account/rateLimits/read' else {}\n"
        "    result = dict(result, pid=os.getpid())\n"
        "    reply = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}\n"
        "    sys.stdout.write(json.dumps(reply) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "    answered += 1\n",
        encoding="utf-8",
    )
    return (sys.executable, str(script))


async def test_aclose_closes_the_child_and_a_later_read_reopens_it(tmp_path: Path) -> None:
    """Against the real `JsonRpcProcess`: close reclaims the child, the next read spawns anew."""
    client = JsonRpcProcess(_stub_app_server(tmp_path, answers_before_exit=100))
    reader = CodexAccountLimitsReader(
        client=client, fallback=FakeRollout(FILE_READING), now=lambda: NOW
    )
    first = await reader.limits_async()
    first_child = client._process
    assert first.windows == (FIVE_HOUR, WEEK)
    assert first_child is not None and first_child.returncode is None
    await reader.aclose()
    assert client._process is None
    assert first_child.returncode is not None, "aclose reclaimed the child"
    second = await reader.limits_async()
    assert second.windows == (FIVE_HOUR, WEEK)
    assert client._process is not None and client._process.pid != first_child.pid
    await reader.aclose()


async def test_a_child_that_died_is_replaced_on_the_read_after_the_fallback(
    tmp_path: Path,
) -> None:
    """Real transport, a child dying with a request pending: fallback once, then a fresh child."""
    client = JsonRpcProcess(_stub_app_server(tmp_path, answers_before_exit=2))
    reader = CodexAccountLimitsReader(
        client=client, fallback=FakeRollout(FILE_READING), now=lambda: NOW
    )
    first = await reader.limits_async()
    assert first.stale_source is None
    dead_child = client._process
    assert dead_child is not None
    second = await reader.limits_async()
    assert second.stale_source == ROLLOUT_STAMP
    third = await reader.limits_async()
    assert third.stale_source is None
    assert third.windows == (FIVE_HOUR, WEEK)
    assert client._process is not None and client._process.pid != dead_child.pid
    await reader.aclose()


# --- Task 1.4: the descriptor's usage reader -------------------------------------------------


def test_a_sync_limits_read_answers_from_the_rollout_file_and_says_so() -> None:
    """A caller that cannot await cannot drive the child; it gets the file, stamped."""
    rollout = FakeRollout(FILE_READING)
    client = FakeClient(recorded())
    limits = CodexAccountLimitsReader(client=client, fallback=rollout, now=lambda: NOW).limits()
    assert limits.windows == FILE_READING.windows
    assert limits.stale_source == ROLLOUT_STAMP
    assert client.calls == [], "the sync path never touches the transport"
    assert rollout.calls == 1


def test_a_session_read_is_delegated_to_the_rollout_reader_untouched(tmp_path: Path) -> None:
    rollout = FakeRollout(FILE_READING)
    reader = CodexAccountLimitsReader(client=FakeClient(recorded()), fallback=rollout)
    query = UsageQuery(ProfileId("codex"), tmp_path, NOW, None)
    answer = reader.read(query)
    assert rollout.queries == [query]
    assert answer == AgentUsage(observed_at=FILE_MOMENT)
