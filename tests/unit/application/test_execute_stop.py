"""One stop dispatch, driven the way both frontends will drive it.

Until now the re-read, the policy re-check and the three-branch dispatch existed twice —
`adapters/telegram/stops.py: StopController.execute` and `adapters/tui/app.py: _issue_stop`
with its caller `stop`. They were the same sequence, and the comment on the bot's copy said
so outright, naming the TUI as "the sibling it was asymmetric with". Two copies of the one
path that ends a session is the arrangement this file exists to retire.

**What is asserted here is the sequence, not just the outcome.** DEC-007's fourth mitigation
and DEC-008's 2026-08-08 correction both turn on the record being re-read and the policy
re-checked *at issue time* rather than trusted from the drawn row, so a test that only
checked "graceful called graceful_stop" would pass over a dispatch that skipped both. Hence
`_Reader` counts its reads and `_UseCase` records its calls in order.

**The unrecognised action raises.** It cannot be reached — the `available_actions` re-check
above admits only the three — but "anything I do not recognise is a kill" is a fail-dangerous
default in the one function that kills, and both surfaces had already removed it separately.
Merging them is the moment that guarantee could quietly be dropped, so it is pinned.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from stop_results import (
    a_clean_stop,
    a_force_stop_that_found_nothing,
    a_stop_that_did_not_take,
    a_verified_force_stop,
)

from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
)
from remote_agents.application.session_actions import GRACEFUL_TIMEOUT, UNKNOWN_SESSION
from remote_agents.application.stops import (
    IDENTITY,
    MISSING,
    UNAVAILABLE,
    StopOutcome,
    dispatch_stop,
    execute_stop,
    resolve_stop,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

SESSION = SessionId(UUID(int=1))
OTHER_SESSION = SessionId(UUID(int=2))
CLAUDE = ProfileId("claude")
CODEX = ProfileId("codex")


def a_record(
    state: SessionState = SessionState.RUNNING,
    *,
    session_id: SessionId = SESSION,
    profile_id: ProfileId = CLAUDE,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        project_id=ProjectId("demo"),
        profile_id=profile_id,
        display=SessionDisplayIdentity("demo", "claude", "interactive", 1),
        state=state,
        created_at=datetime(2026, 8, 22, tzinfo=UTC),
    )


class _Reader:
    """A re-read that can be counted, and that answers differently the second time.

    The count is the assertion: the whole point of the re-read is that the record reaching
    the dispatch is the one the store holds *now*, not the one a surface drew earlier.
    """

    def __init__(self, *records: SessionRecord | None) -> None:
        self._records = list(records)
        self.reads = 0

    async def __call__(self) -> SessionRecord | None:
        self.reads += 1
        return self._records[min(self.reads - 1, len(self._records) - 1)]


class _UseCase:
    """Stands in for `SessionService`, recording which command each branch actually sent."""

    def __init__(
        self,
        *,
        graceful=None,
        force=None,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self._graceful = graceful if graceful is not None else a_clean_stop()
        self._force = force if force is not None else a_verified_force_stop()

    async def graceful_stop(self, command: GracefulStopCommand):
        self.calls.append(("graceful", command))
        return self._graceful

    async def cleanup(self, command: CleanupCommand) -> None:
        self.calls.append(("cleanup", command))

    async def force_stop(self, command: ForceStopCommand):
        self.calls.append(("force", command))
        return self._force


# The record is re-read and the policy re-checked before anything is dispatched -------------


async def test_the_record_is_re_read_before_the_policy_is_checked() -> None:
    reader = _Reader(a_record())
    use_case = _UseCase()

    outcome = await execute_stop("graceful", SESSION, sessions=use_case, read_record=reader)

    assert reader.reads == 1
    assert outcome.dispatched is True
    assert outcome.record is not None


async def test_a_record_that_moved_on_since_the_row_was_drawn_refuses_the_dispatch() -> None:
    """DEC-007/DEC-008: the drawn row offered graceful; the store now says ENDED."""
    reader = _Reader(a_record(SessionState.ENDED))
    use_case = _UseCase()

    outcome = await execute_stop("graceful", SESSION, sessions=use_case, read_record=reader)

    assert reader.reads == 1
    assert use_case.calls == []
    assert outcome.dispatched is False
    assert outcome.refusal == UNAVAILABLE
    # The record comes back so the frontend can say *why* — the refusal wording on both
    # surfaces reads the state, and re-reading it a second time to render the message is
    # what the merge is supposed to remove.
    assert outcome.record is not None
    assert outcome.record.state is SessionState.ENDED


async def test_a_session_the_store_no_longer_has_is_missing_not_merely_unavailable() -> None:
    """The two refusals are distinct because the two surfaces word them differently."""
    reader = _Reader(None)
    use_case = _UseCase()

    outcome = await execute_stop("graceful", SESSION, sessions=use_case, read_record=reader)

    assert use_case.calls == []
    assert outcome.dispatched is False
    assert outcome.refusal == MISSING
    assert outcome.record is None


# DEC-006 — an unknown profile fails closed ------------------------------------------------


async def test_a_profile_that_is_not_the_one_offered_fails_closed() -> None:
    """DEC-006: a stop must not depend on the process that launched it, and must not guess.

    The bot carries the profile through the callback token, so a token minted against
    `claude` reaching a record now stamped `codex` means the identity behind the press is
    not the identity in the store. Nothing is dispatched.
    """
    reader = _Reader(a_record(profile_id=CODEX))
    use_case = _UseCase()

    outcome = await execute_stop(
        "graceful", SESSION, sessions=use_case, read_record=reader, profile_id=CLAUDE
    )

    assert use_case.calls == []
    assert outcome.dispatched is False
    assert outcome.refusal == IDENTITY


async def test_a_reader_that_answers_with_a_different_session_fails_closed() -> None:
    reader = _Reader(a_record(session_id=OTHER_SESSION))
    use_case = _UseCase()

    outcome = await execute_stop("graceful", SESSION, sessions=use_case, read_record=reader)

    assert use_case.calls == []
    assert outcome.dispatched is False
    assert outcome.refusal == IDENTITY


async def test_a_matching_profile_dispatches_and_carries_the_record_s_own_identity() -> None:
    """The command is built from the re-read record, never from the caller's arguments."""
    reader = _Reader(a_record())
    use_case = _UseCase()

    outcome = await execute_stop(
        "graceful", SESSION, sessions=use_case, read_record=reader, profile_id=CLAUDE
    )

    assert outcome.dispatched is True
    name, command = use_case.calls[0]
    assert name == "graceful"
    assert command == GracefulStopCommand(SESSION, CLAUDE)


# The three branches, and what each one reads its failure from -----------------------------


async def test_graceful_reads_its_failure_from_stop_failure() -> None:
    reader = _Reader(a_record())
    use_case = _UseCase(graceful=a_stop_that_did_not_take(GRACEFUL_TIMEOUT))

    outcome = await execute_stop("graceful", SESSION, sessions=use_case, read_record=reader)

    assert outcome.dispatched is True
    assert outcome.failure is not None
    assert outcome.failure.detail == GRACEFUL_TIMEOUT


async def test_a_graceful_stop_that_worked_reports_no_failure() -> None:
    reader = _Reader(a_record())
    use_case = _UseCase(graceful=a_clean_stop())

    outcome = await execute_stop("graceful", SESSION, sessions=use_case, read_record=reader)

    assert outcome.dispatched is True
    assert outcome.failure is None


async def test_cleanup_dispatches_and_has_nothing_to_report() -> None:
    reader = _Reader(a_record(SessionState.PRESERVED))
    use_case = _UseCase()

    outcome = await execute_stop("cleanup", SESSION, sessions=use_case, read_record=reader)

    assert use_case.calls == [("cleanup", CleanupCommand(SESSION))]
    assert outcome.dispatched is True
    assert outcome.failure is None


async def test_force_reads_force_stop_failure_and_never_stop_failure() -> None:
    """DEC-017. `stop_failure` keys on `preserved`, which force makes false on every outcome.

    Routing force through it would report every completed kill as a failure — so a
    successful force must come back with no failure at all, which is the assertion that
    fails if the merge collapses the two readers into one.
    """
    reader = _Reader(a_record())
    use_case = _UseCase(force=a_verified_force_stop())

    outcome = await execute_stop("force", SESSION, sessions=use_case, read_record=reader)

    assert use_case.calls == [("force", ForceStopCommand(SESSION))]
    assert outcome.dispatched is True
    assert outcome.failure is None


async def test_a_force_that_found_no_pane_still_dispatched_and_says_what_it_observed() -> None:
    """DEC-017's accepted cost: the record still ends; only the claim of a kill is dropped."""
    reader = _Reader(a_record())
    use_case = _UseCase(force=a_force_stop_that_found_nothing())

    outcome = await execute_stop("force", SESSION, sessions=use_case, read_record=reader)

    assert outcome.dispatched is True
    assert outcome.failure is not None
    assert outcome.failure.detail == "ownership_lost"


# The fail-dangerous default stays gone ----------------------------------------------------


async def test_an_unrecognised_action_raises_rather_than_force_stopping(monkeypatch) -> None:
    """The policy is monkeypatched because that is the only way to reach this branch.

    `available_actions` never returns `retire`, so in production the refusal below is what
    fires and this raise is unreachable — which is exactly the condition under which a
    fail-dangerous `else` survives unnoticed. It did: the TUI dropped its own on 2026-08-09
    and the bot's stood until 2026-08-15, found by a gate's adversarial pass rather than by
    anything that ran. The scenario being pinned is the
    one the code comments on both retired copies describe: **a future non-destructive
    member of `available_actions`**, offered by the policy and absent from the dispatch. It
    must raise, not fall through to the kill.

    Neither surface's raise had a test before this one; the two comments asserting the
    guarantee were the whole of its coverage.
    """
    reader = _Reader(a_record())
    use_case = _UseCase()
    monkeypatch.setattr(
        "remote_agents.application.stops.available_actions",
        lambda state, provenance: ("graceful", "retire", "force"),
    )

    with pytest.raises(ValueError, match="retire"):
        await execute_stop("retire", SESSION, sessions=use_case, read_record=reader)

    assert use_case.calls == []


async def test_an_unrecognised_action_is_refused_by_the_policy_before_it_can_raise() -> None:
    """The raise is a backstop, not the guard: `available_actions` never offers `retire`.

    Both claims matter and they are different. The refusal above is what an owner meets; the
    raise is what a future caller meets if a new action is added to the policy and not to
    the dispatch. This asserts the ordering — that the policy is consulted first — which is
    what makes the raise unreachable in production rather than merely unlikely.
    """
    reader = _Reader(a_record(SessionState.ENDED))
    use_case = _UseCase()

    outcome = await execute_stop("retire", SESSION, sessions=use_case, read_record=reader)

    assert outcome.dispatched is False
    assert outcome.refusal == UNAVAILABLE
    assert use_case.calls == []


async def test_the_use_case_is_never_touched_on_any_refusal_path() -> None:
    """One sweep over every refusal, so a new one added later cannot dispatch by omission."""
    for reader in (
        _Reader(None),
        _Reader(a_record(SessionState.ENDED)),
        _Reader(a_record(session_id=OTHER_SESSION)),
        _Reader(a_record(profile_id=CODEX)),
    ):
        use_case = _UseCase()
        outcome = await execute_stop(
            "graceful", SESSION, sessions=use_case, read_record=reader, profile_id=CLAUDE
        )
        assert outcome.dispatched is False
        assert use_case.calls == []


async def test_an_unknown_graceful_detail_is_still_reported_as_a_failure() -> None:
    """The generic branch of `stop_failure`, reached through the merged dispatch."""
    reader = _Reader(a_record())
    use_case = _UseCase(graceful=a_stop_that_did_not_take("something-nobody-curated"))

    outcome = await execute_stop("graceful", SESSION, sessions=use_case, read_record=reader)

    assert outcome.dispatched is True
    assert outcome.failure is not None
    assert outcome.failure.detail == "something-nobody-curated"


async def test_the_unknown_session_detail_reaches_the_owner_s_vocabulary() -> None:
    reader = _Reader(a_record())
    use_case = _UseCase(graceful=a_stop_that_did_not_take(UNKNOWN_SESSION))

    outcome = await execute_stop("graceful", SESSION, sessions=use_case, read_record=reader)

    assert outcome.failure is not None
    assert outcome.failure.detail == UNKNOWN_SESSION


# Task 1.3 — the two phases, because the surfaces differ about what happens between them ----
#
# The bot shows a pending notice *before* the whole thing and lands one screen after it, so
# `execute_stop` is exactly its shape. The local surface covers its rows with a spinner
# (`ChoiceScreen.awaiting`) around the dispatch **and not around the re-read** — deliberately:
# that context manager's own docstring says it "covers only the part where something outside
# this process has been asked and has not replied", and a refusal that flashed it would be a
# visible change on a surface this refactor may not change.
#
# Folding the phases into one call would therefore have forced a choice between a spinner on
# every refusal and a second store read. Splitting them costs neither, and cannot drift,
# because `execute_stop` *is* the composition — asserted below rather than asserted about.


async def test_resolve_refuses_without_reaching_the_use_case() -> None:
    reader = _Reader(a_record(SessionState.ENDED))

    resolution = await resolve_stop("graceful", SESSION, read_record=reader)

    assert resolution.refusal == UNAVAILABLE
    assert resolution.record is not None
    assert reader.reads == 1


async def test_resolve_authorises_a_permitted_action_and_carries_the_re_read_record() -> None:
    reader = _Reader(a_record())

    resolution = await resolve_stop("graceful", SESSION, read_record=reader)

    assert resolution.refusal is None
    assert resolution.record is not None
    assert resolution.action == "graceful"


async def test_dispatch_sends_the_command_the_resolution_authorised() -> None:
    resolution = await resolve_stop("graceful", SESSION, read_record=_Reader(a_record()))
    use_case = _UseCase()

    outcome = await dispatch_stop(resolution, sessions=use_case)

    assert outcome.dispatched is True
    assert use_case.calls == [("graceful", GracefulStopCommand(SESSION, CLAUDE))]


async def test_dispatch_refuses_to_run_a_resolution_that_was_refused() -> None:
    """Fail closed. A caller that ignores the refusal and dispatches anyway is the whole
    hazard of splitting the phases, so the second phase re-checks rather than trusting."""
    resolution = await resolve_stop("graceful", SESSION, read_record=_Reader(None))
    use_case = _UseCase()

    with pytest.raises(ValueError, match="refused"):
        await dispatch_stop(resolution, sessions=use_case)

    assert use_case.calls == []


@pytest.mark.parametrize(
    "reader_records, action, observations",
    [
        ((a_record(),), "graceful", {}),
        ((a_record(SessionState.PRESERVED),), "cleanup", {}),
        ((a_record(),), "force", {}),
        ((a_record(SessionState.ENDED),), "graceful", {}),
        ((None,), "graceful", {}),
        # The cases that make the `failure` comparison mean something. Without one of these
        # every parameter set above answers `failure is None`, so comparing the field is
        # `None == None` five times — a fix for an under-assertion that was itself an
        # under-assertion. A force that found no pane is included because force reads a
        # *different* function (DEC-017), so a composition that routed one phase through
        # `stop_failure` would show up here and nowhere else in this test.
        ((a_record(),), "graceful", {"graceful": a_stop_that_did_not_take(GRACEFUL_TIMEOUT)}),
        ((a_record(),), "graceful", {"graceful": a_stop_that_did_not_take(UNKNOWN_SESSION)}),
        ((a_record(),), "force", {"force": a_force_stop_that_found_nothing()}),
    ],
)
async def test_execute_is_exactly_resolve_then_dispatch(
    reader_records, action, observations
) -> None:
    """The seam cannot drift, because the one-call form is built from the two-call form.

    Asserted over every outcome shape — dispatched clean, dispatched with each kind of
    failure, policy-refused, and record-missing — so a future change that special-cases any
    one of them inside `execute_stop` fails here.
    """
    one_call = await execute_stop(
        action,
        SESSION,
        sessions=_UseCase(**observations),
        read_record=_Reader(*reader_records),
    )

    two_calls_use_case = _UseCase(**observations)
    resolution = await resolve_stop(action, SESSION, read_record=_Reader(*reader_records))
    two_calls = (
        await dispatch_stop(resolution, sessions=two_calls_use_case)
        if resolution.refusal is None
        else StopOutcome(False, resolution.record, resolution.refusal)
    )

    # Every field, by value. An earlier version compared `dispatched` and `refusal` by value
    # but `record` only by `is None` — and `failure` not at all — so a composition that
    # returned the *wrong* non-None record, or that diverged on the failure it reported, would
    # have passed the one test whose entire purpose is to catch that. Found by the Stage 1
    # gate's Tier-2 review, which is the pass that reads a claim against its own assertions.
    assert one_call.dispatched == two_calls.dispatched
    assert one_call.refusal == two_calls.refusal
    assert one_call.record == two_calls.record
    assert one_call.failure == two_calls.failure


def test_a_stop_outcome_refuses_to_be_a_bool() -> None:
    """Carried over from the retired `StopResult`, and pinned rather than assumed.

    A dataclass instance is unconditionally truthy, so `assert await execute_stop(...)` would
    pass over every refusal. That is not hypothetical: the same slip survived a commit on the
    type this one replaces, and Task 1.2 rewrote thirteen call sites by hand.
    """
    with pytest.raises(TypeError, match="no truth value"):
        bool(StopOutcome(False))


#: What an adapter may not construct. The three commands are the dispatch itself. **The
#: fourth is the one a reader would not think of**, and it was found by the Stage 1 gate's
#: evaluator: `StopResolution` is `dispatch_stop`'s only argument, and `dispatch_stop`
#: rejects one whose `refusal` is set — but not one hand-built with `refusal=None` around a
#: record the adapter already had. That route constructs the command *inside* `application/`,
#: where the first three names would never appear, so the sweep would stay green while a
#: drawn row reached a kill. Nothing does this today; the point of a safety net is that it
#: covers the move before somebody makes it.
_FORBIDDEN_IN_ADAPTERS = (
    "GracefulStopCommand(",
    "CleanupCommand(",
    "ForceStopCommand(",
    "StopResolution(",
)


def test_no_adapter_builds_a_stop_command_of_its_own() -> None:
    """The three typed commands are constructed in `application/` and nowhere else.

    This is the Stage 1 gate's own sweep, written as a test so the guarantee outlives the
    gate that checked it once. A stop command built inside an adapter is a second dispatch by
    definition — it is the thing the merged use case exists to be the only one of — and a
    grep of this exact shape would have found the two copies this sub-plan retired on any day
    they both existed.

    Deferred from Task 1.2 rather than narrowed to the Telegram tree: it went red there on
    `adapters/tui/app.py`, which was Task 1.3's half, and a test that changes meaning between
    two commits is worse than one that arrives a commit later.
    """
    adapters = pathlib.Path(__file__).resolve().parents[3] / "src" / "remote_agents" / "adapters"
    assert adapters.is_dir(), "the sweep must fail loudly rather than pass over nothing"

    offenders = {
        path.relative_to(adapters).as_posix(): sorted(found)
        for path, found in (
            (path, {name for name in _FORBIDDEN_IN_ADAPTERS if name in path.read_text("utf-8")})
            for path in sorted(adapters.rglob("*.py"))
        )
        if found
    }
    assert offenders == {}
