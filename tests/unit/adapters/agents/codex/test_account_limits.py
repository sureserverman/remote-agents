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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.codex.account_limits import CodexAccountLimitsReader
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import LimitsAbsence, UsageWindow

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
