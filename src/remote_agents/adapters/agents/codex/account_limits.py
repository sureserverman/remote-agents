"""Codex's account limits, asked of its own app server over `account/rateLimits/read`.

`CodexUsageReader.limits` reads the account's windows off whichever rollout was written most
recently, and that answer is only as current as the owner's last Codex turn: an idle host
carries yesterday's percentages against windows that have since moved. This reader asks
instead. `codex app-server` exposes `account/rateLimits/read`, which returns the plan's
figures as of the moment it is asked, for the whole account, whatever any session did — so
the answer is dated *now* and never "when a file was last written" (DEC-061, amended by
sub-plan 01 to let Codex limits be asked of Codex's own local process).

**The transport is a fresh `codex app-server` child, not the daemon's proxy.** DEC-072
accepted a file read for Remote Control because no RPC existed for that question, and because
`codex app-server proxy` never answered `initialize`. Both facts are still true, and neither
applies here: this RPC exists, and a freshly spawned `("codex", "app-server")` answers it
(captured 2026-09-13 on codex-cli 0.154.0 — the fixture under
`tests/provider_contract/fixtures/codex/account_rate_limits/`). The child is owned by this
reader, spawned lazily by `JsonRpcProcess` on the first request, and reclaimed by `aclose`.

**The method name lives here and nowhere else** (DEC-070): one package, one descriptor, one
registry entry, and the string `account/rateLimits/read` in exactly one module.

**The client is injected**, as every collaborator that spawns a `codex` is in this vertical,
so nothing below `tests/live` runs a real process — a test hands in a fake with the same
`request`/`close` surface. `excludeResetCreditDetails` is sent `true` because the credit
ledger is not a rate-limit window and this reader has no use for it.

**Absent is a first-class answer.** The response names its buckets under
`rateLimitsByLimitId`, and `codex` is the plan's own; the top-level `rateLimits` is the same
figures under an older shape, kept as the fallback for a server that omits the map. A
response with neither, or one whose bucket states no window, is `NO_READING` — Codex does
publish limits, none was read this time — and never an invented window.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from remote_agents.adapters.agents.codex.usage import _window_label
from remote_agents.adapters.agents.protocols import JsonRpcProcess
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsageWindow
from remote_agents.ports.agent_usage_support import _instant, _moment, _window

_METHOD = "account/rateLimits/read"
_PARAMS: Mapping[str, object] = {"excludeResetCreditDetails": True}

#: The bucket that is the plan's own; the others (`base_model_inference` on 0.154.0) are
#: per-model reserves the owner is not shown.
_PLAN_BUCKET = "codex"


class RateLimitsClient(Protocol):
    """The two things this reader needs of a JSON-RPC session; `JsonRpcProcess` is one."""

    async def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]: ...

    async def close(self) -> None: ...


class CodexAccountLimitsReader:
    """Read the account's two rate-limit windows by asking Codex's app server.

    The shape of the answer mirrors `CodexUsageReader.limits` so the registry can hold either
    behind one name: `profiles`, `limits_profile`, and an `AgentLimits` filed under `codex`.
    The read is a coroutine because the transport is a child process, which is the one
    difference a caller can see — the registry's `account_limits()` awaits `limits_async`.
    """

    profiles = frozenset({ProfileId("codex")})

    limits_profile = ProfileId("codex")

    def __init__(self, *, client: RateLimitsClient | None = None, now: object = None) -> None:
        self._client = client or JsonRpcProcess(("codex", "app-server"))
        self._now = now

    async def limits_async(self) -> AgentLimits:
        """Ask once, map the plan's bucket, and date the answer at the instant it was read.

        A `ProtocolError` from the transport propagates: the registry files a reader that
        raised as `UNREADABLE`, which is the honest word for it. The rollout-file fallback
        and the restart-after-failure belong to the next task and land between the request
        and `_limits_from`, so the mapping is kept separate from the asking.
        """
        response = await self._client.request(_METHOD, _PARAMS)
        return self._limits_from(response)

    def _limits_from(self, response: Mapping[str, object]) -> AgentLimits:
        windows = _account_windows(_plan_bucket(response), now=self._now)
        if not windows:
            return AgentLimits(self.limits_profile, absence=LimitsAbsence.NO_READING)
        return AgentLimits(self.limits_profile, windows, observed_at=_moment(self._now))

    async def aclose(self) -> None:
        """Reclaim the app-server child this reader may have spawned."""
        await self._client.close()


def _plan_bucket(response: Mapping[str, object]) -> object:
    """The plan's own figures: the `codex` bucket when the map carries it, else the top level."""
    by_limit_id = response.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        bucket = by_limit_id.get(_PLAN_BUCKET)
        if isinstance(bucket, dict):
            return bucket
    return response.get("rateLimits")


def _account_windows(bucket: object, *, now: object = None) -> tuple[UsageWindow, ...]:
    """Map `primary`/`secondary` the way the rollout reader does, under the RPC's own names.

    The app server speaks camelCase (`usedPercent`, `windowDurationMins`, `resetsAt`) where
    the rollout writes snake_case, and the label is still derived from the stated duration
    rather than from the position, for the reason `_codex_windows` records. `_window` drops a
    window whose reset has already passed.
    """
    if not isinstance(bucket, dict):
        return ()
    windows = []
    for key in ("primary", "secondary"):
        section = bucket.get(key)
        if not isinstance(section, dict):
            continue
        window = _window(
            _window_label(section.get("windowDurationMins")),
            section.get("usedPercent"),
            _instant(section.get("resetsAt")),
            now=now,
        )
        if window is not None:
            windows.append(window)
    return tuple(windows)
