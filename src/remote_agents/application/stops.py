"""The one stop dispatch, for both frontends.

Ending a session was written twice. `adapters/telegram/stops.py: StopController.execute` and
`adapters/tui/app.py: stop` + `_issue_stop` performed the same four steps — re-read the
record, re-check the policy, send exactly one curated command, interpret what came back —
against the same `application.session_actions` vocabulary, and drifted anyway: the TUI
removed a fail-dangerous trailing `else` from its dispatch and the bot kept one for months
afterwards, until a gate's adversarial pass found the asymmetry. Two copies of the only path
that destroys a session is the arrangement this module retires (ARCH-B4).

**The re-read lives here, not in the caller.** DEC-007's fourth mitigation and DEC-008's
2026-08-08 correction both say the same thing: what refuses a second stop is re-reading the
record and re-checking the policy *at issue time*, never an in-flight flag and never the row
the surface drew. Leaving the re-read outside would let a frontend hand in a remembered
record and get a dispatch, which is precisely the shape those decisions forbid — so the
caller supplies a `read_record` callable and this function decides when to use it.

**It never asks a question.** DEC-025 is explicit that a confirmation is only ever awaited
from a screen's own handler, so a shared use case taking a confirmation callback would be the
forbidden shape with an extra step. `execute_stop` receives an already-decided action and
returns a value; the two-press force confirmation stays in the Telegram controller and the
modal stays in the Textual screen (ARCH-B4).

**It reports rather than renders.** Both frontends word their refusals differently — the bot
lands one notice on the sessions list, the surface has one sentence for a vanished record and
another naming the state that refused — so the outcome carries *which* refusal happened and
the record that explains it, and each surface writes its own sentence from that.

What is deliberately still per-surface: the console step, which is not here at all. It hangs
off `SessionService` at composition time (`_hide_in_console`, capped at 2s per DEC-040), so
both processes get whatever their own composer wired and neither dispatch has to know.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
)
from remote_agents.application.session_actions import (
    CLEANUP,
    FORCE,
    GRACEFUL,
    StopFailure,
    available_actions,
    force_stop_failure,
    stop_failure,
)
from remote_agents.domain.models import ProfileId, SessionId, SessionRecord

MISSING = "missing"
"""The store has no record for this session at all.

It ended and was cleared, or the id is stale."""

IDENTITY = "identity"
"""The re-read record is not the session or the profile the request named (DEC-006).

Fails closed rather than dispatching against whichever record came back. The bot carries the
profile through its callback token, so a token minted against one profile meeting a record
stamped with another means the identity behind the press is not the identity in the store —
and a stop that guesses which one it meant is the failure DEC-006 exists to prevent.
"""

UNAVAILABLE = "unavailable"
"""The policy does not offer this action from the state the record is in *now*."""


class _StopUseCase(Protocol):
    """The three methods a stop dispatch needs; `SessionService` satisfies it."""

    async def graceful_stop(self, command: GracefulStopCommand): ...

    async def cleanup(self, command: CleanupCommand) -> None: ...

    async def force_stop(self, command: ForceStopCommand): ...


@dataclass(frozen=True, slots=True)
class StopOutcome:
    """What the dispatch did, and everything either frontend needs to say so.

    Four fields rather than a bool because the callers ask four different questions of it,
    and the two surfaces answer two of them with different sentences.

    `dispatched` is false on every refusal; `refusal` says which one, because the local
    surface words a vanished record ("that session is no longer available") differently from
    a policy refusal (which names the state and explains it) while the bot collapses both
    into one notice. `record` is the **re-read** one and is present on every path that got
    far enough to have it, so a frontend renders its refusal from the record that caused it
    rather than reading the store a second time. `failure` is populated only on a dispatch,
    and only when the command did not take effect.
    """

    dispatched: bool
    record: SessionRecord | None = None
    refusal: str | None = None
    failure: StopFailure | None = None


async def execute_stop(
    action: str,
    session_id: SessionId,
    *,
    sessions: _StopUseCase,
    read_record: Callable[[], Awaitable[SessionRecord | None]],
    profile_id: ProfileId | None = None,
) -> StopOutcome:
    """Re-read, re-check, dispatch exactly one curated command, and report what it did.

    `read_record` is the caller's own store read — the bot's list lookup and the surface's
    `current_record` read different ways and neither should become the other's — but *when*
    it runs is this function's decision, which is what keeps a remembered record from
    reaching a dispatch.

    `profile_id` is checked when given and ignored when not. The bot has one, carried
    through the callback token that offered the action; the local surface acts on the record
    under the cursor and has nothing separate to compare. Supplying it buys the DEC-006
    fail-closed check, and the command is built from the **record's** identity either way —
    after the check the two agree, and preferring the re-read value is the safer default for
    the caller that has nothing to compare.

    **Force is read through `force_stop_failure`, not `stop_failure`, and that is not an
    inconsistency.** The latter keys on `preserved`, which is a graceful stop's success and
    is false on *every* force including the one that worked, because force removes the pane
    rather than keeping it. What force reports instead is the case DEC-017 names: no managed
    pane matched, nothing was killed, and the service recorded `VERIFIED_FORCE_STOP` anyway
    so the record still reaches ENDED and the row still clears. The session does end; the
    claim that a kill was observed is what stops being made.
    """
    record = await read_record()
    if record is None:
        return StopOutcome(False, refusal=MISSING)
    if record.session_id != session_id or (
        profile_id is not None and record.profile_id != profile_id
    ):
        return StopOutcome(False, record, IDENTITY)
    if action not in available_actions(record.state, record.orphan_provenance):
        return StopOutcome(False, record, UNAVAILABLE)

    if action == GRACEFUL:
        observation = await sessions.graceful_stop(
            GracefulStopCommand(record.session_id, record.profile_id)
        )
        return StopOutcome(True, record, failure=stop_failure(observation))
    if action == CLEANUP:
        await sessions.cleanup(CleanupCommand(record.session_id))
        return StopOutcome(True, record)
    if action == FORCE:
        observation = await sessions.force_stop(ForceStopCommand(record.session_id))
        return StopOutcome(True, record, failure=force_stop_failure(observation))
    # Force is its own named branch and an unrecognised action raises, rather than the kill
    # being the trailing `else`. Nothing reaches this today — the re-check above admits only
    # the three — but "anything I do not recognise is a kill" is a fail-dangerous default in
    # the one function that kills, and any future non-destructive member of
    # `available_actions` would silently have become a force stop. Both retired copies had
    # removed this separately, months apart; merging them is the moment it could quietly
    # come back, so the guarantee is pinned by a test rather than by this comment.
    raise ValueError(f"no command is curated for the action {action!r}")
