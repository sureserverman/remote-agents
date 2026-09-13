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

**When the child cannot answer, the rollout file does, and says so.** A `ProtocolError`
(timed out, closed, no object result), a `TimeoutError`, a `FileNotFoundError` (no `codex` on
the path) and any other `OSError` from spawning or talking to the child all answer with
`CodexUsageReader.limits()`'s reading stamped `stale_source="rollout file"` — DEC-061's stamp
rule: a figure that did not come from the moment it was asked for is never rendered as though
it had. The fallback's own `observed_at` is kept, because that is the instant the file's
figures describe. The failed child is discarded on the spot, so the *next* read spawns a
fresh one rather than re-asking a wedged process; the transport is restarted, never abandoned.

**Absent is a first-class answer.** The response names its buckets under
`rateLimitsByLimitId`, and `codex` is the plan's own; the top-level `rateLimits` is the same
figures under an older shape, kept as the fallback for a server that omits the map. A
response with neither, or one whose bucket states no window, is `NO_READING` — Codex does
publish limits, none was read this time — and never an invented window.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

from remote_agents.adapters.agents.codex.usage import CodexUsageReader, _window_label
from remote_agents.adapters.agents.protocols import JsonRpcProcess, ProtocolError
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    LimitsAbsence,
    UsageQuery,
    UsageWindow,
)
from remote_agents.ports.agent_usage_support import _instant, _moment, _window

_METHOD = "account/rateLimits/read"
_PARAMS: Mapping[str, object] = {"excludeResetCreditDetails": True}

#: The bucket that is the plan's own; the others (`base_model_inference` on 0.154.0) are
#: per-model reserves the owner is not shown.
_PLAN_BUCKET = "codex"

#: What the fallback's reading is stamped with, in the owner's words (DEC-061).
_ROLLOUT_STAMP = "rollout file"

#: Everything the child can do short of answering. `FileNotFoundError` (no `codex` binary) and
#: `TimeoutError` (the budget below, or the transport's own) are both `OSError`s on this
#: interpreter, so the pair below is the whole set; the subclasses are named in the docstring
#: rather than here, where listing one beside its parent would suggest they are handled apart.
_TRANSPORT_FAULTS = (ProtocolError, OSError)

#: How long one read may wait on the child, spawn and `initialize` included. Measured on the
#: owner's host 2026-09-13: cold spawn + initialize + read 0.57-0.69 s, warm read 0.44-0.64 s,
#: so this is many times the ordinary case and still short of a render anyone notices.
_RPC_BUDGET_SECONDS = 5.0

#: How long a failed child gets to go away before the read stops waiting for it. The client's
#: own close already escalates to SIGKILL inside this; the bound is for a close that hangs
#: before it gets there, and `JsonRpcProcess.close` kills the child when cancelled.
_DISCARD_BUDGET_SECONDS = 5.0

#: How long one answer stands before the child is asked again -- live or fallback alike.
#: The bot redraws its sessions reply on every store change behind a two-second debounce
#: (`_REDRAW_INTERVAL_SECONDS`), and each redraw draws the limits block; without this a burst
#: of launches asked the app server every two seconds. A window moves by the turn, not by
#: the second, and the terminal already refreshes on this cadence.
_MEMO_SECONDS = 60.0


class RateLimitsClient(Protocol):
    """The two things this reader needs of a JSON-RPC session; `JsonRpcProcess` is one."""

    async def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]: ...

    async def close(self) -> None: ...


class RolloutReader(Protocol):
    """What this reader needs of the file reader; `CodexUsageReader` is one.

    `limits` is the fallback; `read` is the session read this reader never reinterprets --
    a session's context window lives in its rollout and nowhere the app server would say.
    """

    def limits(self) -> AgentLimits: ...

    def read(self, query: UsageQuery) -> AgentUsage | None: ...


class CodexAccountLimitsReader:
    """Read the account's two rate-limit windows by asking Codex's app server.

    The descriptor's `usage` capability, wrapping the rollout reader rather than replacing
    it: `read` delegates a session query to the file untouched, the sync `limits` answers
    from the file stamped as such, and only `limits_async` asks the child -- the one
    difference a caller can see, and the reason `ProfileUsageReaders.account_limits()` is a
    coroutine. `profiles` and `limits_profile` match the rollout reader's own values; they
    are this class's, not read off the injected fallback.

    One answer per minute (`_MEMO_SECONDS`), whichever source gave it, and every wait on the
    child bounded (`_RPC_BUDGET_SECONDS`, `_DISCARD_BUDGET_SECONDS`): a wedged host costs
    one bounded attempt a minute and answers from the file in between, never a read per
    redraw on the bot's render path.
    """

    profiles = frozenset({ProfileId("codex")})

    limits_profile = ProfileId("codex")

    def __init__(
        self,
        *,
        client: RateLimitsClient | None = None,
        fallback: RolloutReader | None = None,
        now: object = None,
    ) -> None:
        self._client = client or JsonRpcProcess(("codex", "app-server"))
        self._fallback = fallback if fallback is not None else CodexUsageReader(now=now)
        self._now = now
        self._remembered: tuple[datetime, AgentLimits] | None = None

    async def limits_async(self) -> AgentLimits:
        """Ask once, map the plan's bucket, and date the answer at the instant it was read.

        When the child cannot answer, discard it and answer from the rollout file instead,
        stamped. A fault from the *fallback* itself (an unreadable rollout) propagates: the
        registry files a reader that raised as `UNREADABLE`, which is the honest word for a
        host where neither source could be read.
        """
        asked_at = _moment(self._now)
        if self._remembered is not None:
            remembered_at, answer = self._remembered
            if timedelta(0) <= asked_at - remembered_at < timedelta(seconds=_MEMO_SECONDS):
                return answer
        try:
            response = await asyncio.wait_for(
                self._client.request(_METHOD, _PARAMS), timeout=_RPC_BUDGET_SECONDS
            )
        except _TRANSPORT_FAULTS:
            await self._discard_child()
            answer = await self._from_rollout_file()
        else:
            answer = self._limits_from(response)
        self._remembered = (asked_at, answer)
        return answer

    async def _discard_child(self) -> None:
        """Let go of a child that failed, so the next read spawns a fresh one.

        `JsonRpcProcess` reuses a child whose `returncode` is still `None`, and a child that
        timed out is exactly that -- alive and not answering. Closing it is what makes
        "restarted on the next read" true. Errors on the way out are swallowed: this is
        tidying after a failure already being answered, and the fallback is the answer.
        """
        with suppress(*_TRANSPORT_FAULTS):
            await asyncio.wait_for(self._client.close(), timeout=_DISCARD_BUDGET_SECONDS)

    async def _from_rollout_file(self) -> AgentLimits:
        # On a worker thread for the reason `composition.backend._limits_reader` gave when it
        # threaded the whole read: this sweeps up to `_ACCOUNT_ROLLOUT_DAYS` dated directories.
        reading = await asyncio.to_thread(self._fallback.limits)
        return replace(reading, stale_source=_ROLLOUT_STAMP)

    def read(self, query: UsageQuery) -> AgentUsage | None:
        """A session's usage is the rollout's to answer; nothing here reinterprets it."""
        return self._fallback.read(query)

    def limits(self) -> AgentLimits:
        """The sync answer: the rollout file, stamped -- a caller that cannot await cannot ask.

        Kept so `ProfileUsageReaders.limits()` and the provider contract still read this
        capability the way they read every other; the stamp is what stops a file figure
        reached this way from rendering as though the child had just been asked (DEC-061).
        """
        return replace(self._fallback.limits(), stale_source=_ROLLOUT_STAMP)

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
