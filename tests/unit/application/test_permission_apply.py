"""The pass that sets each launched session's preset on its provider's shared server.

A preset is no longer a launch flag, so a session's pane comes up with the provider's own default
and the service owes it the preset it was launched with. These tests pin what the pass may act on
(the session's own thread, from the binding or the resume id, and nothing else), when it gives up,
and what a caller about to type automatic work is told.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from remote_agents.application.permission_apply import (
    APPLY_PATIENCE_SECONDS,
    ENDED_REASON,
    NOT_IN_TIME_REASON,
    PERMISSIONS_HELD,
    READY,
    RETRY_SECONDS,
    UNBOUND_REASON,
    PermissionApplyPass,
    PermissionFailureWatch,
    Readiness,
    permission_state_line,
    permissions_notice,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_bindings import PermissionState
from remote_agents.ports.thread_permissions import ThreadPermissionsError

PROVIDER = ProfileId("shared-server-provider")
OTHER = ProfileId("no-capability-provider")
SESSION = SessionId(UUID("6f1c2a52-3b7e-4c1d-9a57-0d2f6f1b8e01"))
SECOND = SessionId(UUID("6f1c2a52-3b7e-4c1d-9a57-0d2f6f1b8e02"))
THREAD = "01a1c7a4-4e84-7fb2-b3d2-e60831188225"
DECOY = "01a1c7a4-0000-7000-8000-000000000001"
START = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def _record(
    session_id: SessionId = SESSION,
    *,
    profile: ProfileId = PROVIDER,
    state: SessionState = SessionState.RUNNING,
    resumed_from: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        profile,
        SessionDisplayIdentity("opaque-editor", str(profile), "regular", 1),
        state,
        START,
        resume_profile_id=profile if resumed_from else None,
        resume_source_id=resumed_from,
    )


class FakeServer:
    """A shared server: the threads loaded on it, by folder, and every `apply` it was sent."""

    def __init__(self) -> None:
        self.identity: str | None = "daemon-A"
        self.loaded: dict[str, str] = {THREAD: "/srv/project", DECOY: "/srv/project"}
        self.calls: list[tuple[str, PermissionPreset]] = []
        self.failures: list[str] = []
        """Reasons the next applies fail with, first first."""
        self.during_apply: list = []
        """Awaited one per apply, while it is in flight: what else happens meanwhile."""

    async def apply(self, thread_id: str, preset: PermissionPreset) -> None:
        self.calls.append((thread_id, preset))
        if self.during_apply:
            await self.during_apply.pop(0)()
        if self.failures:
            raise ThreadPermissionsError(self.failures.pop(0))
        if thread_id not in self.loaded:
            raise ThreadPermissionsError(f"thread {thread_id} is not loaded on the daemon")

    async def server_identity(self) -> str | None:
        return self.identity


class FakeBindings:
    """The state half of `PermissionBindings`, in memory, with every write counted."""

    def __init__(self) -> None:
        self.states: dict[SessionId, PermissionState] = {}
        self.writes: list[tuple[str, SessionId]] = []

    def pending(self, session_id: SessionId, preset: PermissionPreset) -> None:
        self.states[session_id] = PermissionState(preset, None, None, None)

    async def state(self, session_id: SessionId) -> PermissionState | None:
        return self.states.get(session_id)

    async def mark_applied(
        self, session_id: SessionId, preset: PermissionPreset, daemon: str
    ) -> None:
        self.writes.append(("applied", session_id))
        self.states[session_id] = replace(
            self.states[session_id], applied=preset, daemon=daemon, failure=None
        )

    async def mark_failed(
        self, session_id: SessionId, reason: str, *, requested: PermissionPreset
    ) -> bool:
        if self.states[session_id].requested is not requested:
            return False
        self.writes.append(("failed", session_id))
        self.states[session_id] = replace(self.states[session_id], failure=reason)
        return True

    async def set_requested(self, session_id: SessionId, preset: PermissionPreset) -> None:
        self.states[session_id] = replace(self.states[session_id], requested=preset, failure=None)


class Rig:
    def __init__(self, *records: SessionRecord) -> None:
        self.records = list(records)
        self.server = FakeServer()
        self.bindings = FakeBindings()
        self.threads: dict[str, str] = {}
        self.gave_up: set[str] = set()
        self.bound_answers: list[bool] = []
        self.bound_asked: list[tuple[SessionId, ProfileId, bool]] = []
        self.told: list[tuple[SessionId, str]] = []
        self.rollover_successors: set[SessionId] = set()
        self.notify_error: Exception | None = None
        self.now = START
        self.slept: list[float] = []
        self.apply_pass = PermissionApplyPass(
            self.bindings,
            sessions=self._sessions,
            appliers={str(PROVIDER): self.server},
            thread_of=self._thread_of,
            unbound=self.gave_up.__contains__,
            bound=self._bound,
            notify=self._notify,
            told_elsewhere=self._told_elsewhere,
            now=lambda: self.now,
            sleep=self._sleep,
        )

    async def _notify(self, record: SessionRecord, reason: str) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.told.append((record.session_id, reason))

    async def _told_elsewhere(self, session_id: SessionId) -> bool:
        return session_id in self.rollover_successors

    def _thread_of(self, session_id: str, profile_id: str) -> str | None:
        assert profile_id == str(PROVIDER)
        return self.threads.get(session_id)

    async def _sessions(self) -> Sequence[SessionRecord]:
        return tuple(self.records)

    async def _bound(self, session_id: SessionId, profile_id: ProfileId, *, tell: bool) -> bool:
        self.bound_asked.append((session_id, profile_id, tell))
        return self.bound_answers.pop(0) if self.bound_answers else True

    async def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)

    async def tick(self, *, seconds: float = RETRY_SECONDS) -> None:
        """One pass, then the clock moves on to the next one."""
        await self.apply_pass.run_once()
        self.now += timedelta(seconds=seconds)


async def test_a_fresh_session_gets_no_call_until_its_thread_is_bound_then_exactly_one() -> None:
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)

    await rig.tick()
    await rig.tick()
    assert rig.server.calls == [], "nothing names the session's thread yet"

    rig.threads[str(SESSION)] = THREAD
    await rig.tick()
    await rig.tick()
    await rig.tick()

    assert rig.server.calls == [(THREAD, PermissionPreset.ASK)]
    assert rig.bindings.states[SESSION] == PermissionState(
        PermissionPreset.ASK, PermissionPreset.ASK, "daemon-A", None
    )


async def test_no_folder_or_time_rule_picks_a_thread() -> None:
    """A SECOND thread sits loaded on the server in the session's own folder, opened at the same
    moment. It is never applied to: only the binding or the resume id names a thread (DEC-128,
    DEC-134), and the pass has no way to ask the server what is loaded."""
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.FULL_ACCESS)
    assert rig.server.loaded[DECOY] == rig.server.loaded[THREAD]

    await rig.tick()
    rig.threads[str(SESSION)] = THREAD
    await rig.tick()

    assert [thread for thread, _ in rig.server.calls] == [THREAD]
    assert not hasattr(rig.apply_pass, "loaded_threads")


async def test_a_resumed_session_is_applied_through_its_resume_id() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.APPROVE_FOR_ME)
    # A binding for some other thread must not win over the conversation that was resumed.
    rig.threads[str(SESSION)] = DECOY

    await rig.tick()

    assert rig.server.calls == [(THREAD, PermissionPreset.APPROVE_FOR_ME)]


async def test_the_provider_default_gets_no_call_and_counts_as_applied() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.PROVIDER_DEFAULT)

    await rig.tick()

    assert rig.server.calls == []
    assert rig.bindings.writes == []
    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == READY


async def test_a_session_with_no_state_row_is_never_touched() -> None:
    """Launched with flags by an older build: its pane runs off the shared server."""
    rig = Rig(_record(resumed_from=THREAD))

    await rig.tick()

    assert rig.server.calls == []
    assert rig.bindings.writes == []
    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == READY


async def test_a_provider_without_the_capability_is_never_touched() -> None:
    rig = Rig(_record(profile=OTHER, resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)

    await rig.tick()

    assert rig.server.calls == []
    assert await rig.apply_pass.wait_ready(SESSION, OTHER) == READY


async def test_an_applied_session_is_left_alone_on_later_passes() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)

    for _ in range(5):
        await rig.tick()

    assert len(rig.server.calls) == 1
    assert rig.bindings.writes == [("applied", SESSION)]


async def test_a_changed_daemon_identity_reapplies_once() -> None:
    """A restarted server has forgotten the setting: the pane is back on the provider default."""
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    await rig.tick()

    rig.server.identity = "daemon-B"
    await rig.tick()
    await rig.tick()
    await rig.tick()

    assert rig.server.calls == [(THREAD, PermissionPreset.ASK)] * 2
    assert rig.bindings.states[SESSION].daemon == "daemon-B"


async def test_an_unreadable_daemon_identity_does_not_reapply_every_pass() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    await rig.tick()

    rig.server.identity = None
    await rig.tick()
    await rig.tick()

    assert len(rig.server.calls) == 1


async def test_a_failure_retries_once_on_the_next_pass_then_stands_with_its_reason() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["timed out", "no Codex daemon", "never reached"]

    await rig.tick()
    assert rig.bindings.states[SESSION].failure is None, "one failure is not yet a verdict"
    await rig.tick()
    assert rig.bindings.states[SESSION] == PermissionState(
        PermissionPreset.ASK, None, None, "no Codex daemon"
    )

    for _ in range(4):
        await rig.tick()

    assert len(rig.server.calls) == 2, "the failure stands: no third try"
    assert rig.bindings.writes == [("failed", SESSION)]


async def test_the_retry_waits_for_the_next_pass_even_when_a_caller_is_waiting() -> None:
    """A caller polling every SECOND must not spend both tries inside one pass interval: a
    thread that is a moment late on the server would then fail for good."""
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["thread is not loaded"]

    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == READY

    assert len(rig.server.calls) == 2
    assert sum(rig.slept) >= RETRY_SECONDS


async def test_a_retry_that_succeeds_leaves_no_failure() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["timed out"]

    await rig.tick()
    await rig.tick()

    assert rig.bindings.states[SESSION] == PermissionState(
        PermissionPreset.ASK, PermissionPreset.ASK, "daemon-A", None
    )


async def test_a_standing_failure_is_tried_again_when_the_daemon_changes() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["no Codex daemon", "no Codex daemon"]
    await rig.tick()
    await rig.tick()
    assert rig.bindings.states[SESSION].failure == "no Codex daemon"

    rig.server.identity = "daemon-B"
    await rig.tick()

    assert rig.bindings.states[SESSION] == PermissionState(
        PermissionPreset.ASK, PermissionPreset.ASK, "daemon-B", None
    )


async def test_a_failure_recorded_before_a_restart_of_the_service_stands() -> None:
    """A new pass has no memory of the tries. It must not start them over, or every service
    restart would tell the owner about the same failure again."""
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.states[SESSION] = PermissionState(PermissionPreset.ASK, None, None, "timed out")

    for _ in range(3):
        await rig.tick()

    assert rig.server.calls == []


async def test_a_new_request_after_a_failure_is_applied() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["timed out", "timed out"]
    await rig.tick()
    await rig.tick()

    await rig.bindings.set_requested(SESSION, PermissionPreset.FULL_ACCESS)
    await rig.tick()

    assert rig.server.calls[-1] == (THREAD, PermissionPreset.FULL_ACCESS)
    assert rig.bindings.states[SESSION] == PermissionState(
        PermissionPreset.FULL_ACCESS, PermissionPreset.FULL_ACCESS, "daemon-A", None
    )


async def test_a_failure_of_a_preset_the_owner_has_since_replaced_is_not_recorded() -> None:
    """An apply can take seconds. If the owner asks for another preset meanwhile, the old
    preset's failure is not news about the new one, and must not stop the new one being tried."""
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["timed out", "timed out"]
    await rig.tick()

    async def the_owner_changes_it() -> None:
        await rig.bindings.set_requested(SESSION, PermissionPreset.FULL_ACCESS)

    rig.server.during_apply = [the_owner_changes_it]
    await rig.tick()

    assert rig.bindings.states[SESSION] == PermissionState(
        PermissionPreset.FULL_ACCESS, None, None, None
    ), "the failure belonged to a request that no longer stands"
    assert rig.bindings.writes == []

    await rig.tick()

    assert rig.server.calls[-1] == (THREAD, PermissionPreset.FULL_ACCESS)
    assert rig.bindings.states[SESSION] == PermissionState(
        PermissionPreset.FULL_ACCESS, PermissionPreset.FULL_ACCESS, "daemon-A", None
    )


async def test_a_binding_that_gave_up_is_not_recorded_against_a_replaced_request() -> None:
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    stale = rig.bindings.states[SESSION]
    await rig.bindings.set_requested(SESSION, PermissionPreset.FULL_ACCESS)

    step = await rig.apply_pass._fail(SESSION, stale, "its thread could not be bound", "daemon-A")

    assert step.value == "waiting"
    assert rig.bindings.states[SESSION].failure is None
    assert rig.apply_pass.tracked == frozenset()


async def test_a_failure_from_before_a_service_restart_is_tried_again_on_a_new_daemon() -> None:
    """The new run learns which daemon the failure stands under on its first look, and tries
    again only once that daemon is another one."""
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.states[SESSION] = PermissionState(PermissionPreset.ASK, None, None, "timed out")
    await rig.tick()
    await rig.tick()
    assert rig.server.calls == []

    rig.server.identity = "daemon-B"
    await rig.tick()

    assert rig.server.calls == [(THREAD, PermissionPreset.ASK)]
    assert rig.bindings.states[SESSION] == PermissionState(
        PermissionPreset.ASK, PermissionPreset.ASK, "daemon-B", None
    )


async def test_a_changed_request_is_applied_over_an_applied_one() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    await rig.tick()

    await rig.bindings.set_requested(SESSION, PermissionPreset.APPROVE_FOR_ME)
    await rig.tick()
    await rig.tick()

    assert rig.server.calls == [
        (THREAD, PermissionPreset.ASK),
        (THREAD, PermissionPreset.APPROVE_FOR_ME),
    ]


async def test_an_ended_session_is_skipped() -> None:
    rig = Rig(_record(state=SessionState.ENDED, resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)

    await rig.tick()

    assert rig.server.calls == []
    assert rig.bindings.writes == []


async def test_a_session_on_its_trust_dialog_waits() -> None:
    """Its agent has not opened a thread on the server yet, so an apply could only fail."""
    rig = Rig(_record(state=SessionState.UNTRUSTED, resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)

    await rig.tick()
    assert rig.server.calls == []

    rig.records[0] = _record(resumed_from=THREAD)
    await rig.tick()
    assert len(rig.server.calls) == 1


async def test_a_fresh_session_whose_binding_gave_up_is_recorded_as_failed_without_a_call() -> None:
    """With no thread there is nothing to apply to, and the pane keeps the provider's default.
    That is a failure the owner must hear, not a session left pending for ever."""
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.gave_up.add(str(SESSION))

    await rig.tick()
    await rig.tick()

    assert rig.server.calls == []
    assert rig.bindings.states[SESSION].failure == UNBOUND_REASON
    assert rig.bindings.writes == [("failed", SESSION)]


async def test_one_session_failing_does_not_stop_the_pass_reaching_the_next() -> None:
    rig = Rig(_record(resumed_from="missing-thread"), _record(SECOND, resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.bindings.pending(SECOND, PermissionPreset.FULL_ACCESS)

    await rig.tick()

    assert rig.bindings.states[SECOND].applied is PermissionPreset.FULL_ACCESS


async def test_an_applier_that_raises_something_else_is_a_failure_not_a_dead_pass() -> None:
    class Broken(FakeServer):
        async def apply(self, thread_id: str, preset: PermissionPreset) -> None:
            self.calls.append((thread_id, preset))
            raise RuntimeError("a traceback with /home/owner/secret in it")

    rig = Rig(_record(resumed_from=THREAD), _record(SECOND, profile=OTHER, resumed_from=THREAD))
    broken = Broken()
    rig.apply_pass = PermissionApplyPass(
        rig.bindings,
        sessions=rig._sessions,
        appliers={str(PROVIDER): broken},
        thread_of=rig._thread_of,
        now=lambda: rig.now,
        sleep=rig._sleep,
    )
    rig.bindings.pending(SESSION, PermissionPreset.ASK)

    await rig.tick()
    await rig.tick()

    assert rig.bindings.states[SESSION].failure == "the apply failed unexpectedly"
    assert len(broken.calls) == 2


async def test_wait_ready_is_bound_then_applied() -> None:
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.threads[str(SESSION)] = THREAD

    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == READY
    assert rig.server.calls == [(THREAD, PermissionPreset.ASK)]


async def test_wait_ready_says_unbound_before_it_tries_to_apply() -> None:
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.bound_answers = [False]

    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == Readiness(
        bound=False, unapplied=NOT_IN_TIME_REASON
    )
    assert rig.server.calls == []


async def test_wait_ready_says_unapplied_once_the_failure_stands() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["timed out", "timed out"]

    held = Readiness(bound=True, unapplied="timed out")
    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == held
    assert rig.bindings.states[SESSION].failure == "timed out"
    # And again, without another try: the failure stands.
    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == held
    assert len(rig.server.calls) == 2


async def test_wait_ready_is_bounded_when_the_session_never_becomes_ready() -> None:
    """Bound by the caller's own check, yet no thread is named: the wait ends, unapplied."""
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)

    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == Readiness(
        bound=True, unapplied=NOT_IN_TIME_REASON
    )

    assert rig.server.calls == []
    assert APPLY_PATIENCE_SECONDS <= sum(rig.slept) <= APPLY_PATIENCE_SECONDS + 1.0


async def test_wait_ready_for_an_ended_session_is_unapplied_at_once() -> None:
    rig = Rig(_record(state=SessionState.ENDED, resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)

    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == Readiness(
        bound=True, unapplied=ENDED_REASON
    )
    assert rig.slept == []


async def test_wait_ready_never_asks_the_binder_about_a_resumed_session() -> None:
    """A resumed session's thread is the conversation it resumed. It asked for no binding, so
    the binder would call it unbound for ever."""
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.bound_answers = [False]

    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == READY
    assert rig.bound_asked == []


async def test_wait_ready_finds_the_profile_itself_when_the_caller_has_only_the_session() -> None:
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.threads[str(SESSION)] = THREAD

    assert await rig.apply_pass.wait_ready(SESSION) == READY
    assert rig.bound_asked == [(SESSION, PROVIDER, False)]
    assert await rig.apply_pass.wait_ready(SECOND) == Readiness(unapplied=ENDED_REASON)


async def test_a_binder_that_fails_reads_as_unbound_and_says_nothing_about_permissions() -> None:
    """So a provider that takes no preset still gets its owner's scheduled prompt, as before."""
    rig = Rig(_record(profile=OTHER))

    async def broken(session_id, profile_id, *, tell):
        raise RuntimeError("the store went away")

    rig.apply_pass._bound = broken

    assert await rig.apply_pass.wait_ready(SESSION, OTHER) == Readiness(bound=False)


async def test_an_unbound_session_that_is_owed_nothing_is_only_unbound() -> None:
    """The provider's own default asks for nothing, so its owner's work may still be typed."""
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.PROVIDER_DEFAULT)
    rig.bound_answers = [False]

    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER) == Readiness(bound=False)


async def test_an_unbound_session_that_is_owed_a_preset_is_unapplied_too_and_says_why() -> None:
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.FULL_ACCESS)
    rig.bound_answers = [False]
    rig.gave_up.add(str(SESSION))

    assert await rig.apply_pass.wait_ready(SESSION, PROVIDER, tell=True) == Readiness(
        bound=False, unapplied=UNBOUND_REASON
    )
    assert rig.bound_asked == [(SESSION, PROVIDER, True)]
    assert rig.slept == [], "one look: the binder has already waited"


async def test_a_session_that_ends_is_forgotten_and_a_reused_key_starts_clean() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["timed out"]
    await rig.tick()

    rig.records.clear()
    await rig.tick()

    assert rig.apply_pass.tracked == frozenset()


# --- Telling the owner (Task 2.4). ---


def test_permissions_notice_names_the_session_and_the_reason_and_no_provider() -> None:
    text = permissions_notice(_record(), "remote-agents", "no Codex daemon")

    assert text == (
        "shared-server-provider in remote-agents #1 could not be given its permissions: "
        "no Codex daemon. It runs on the agent's own configuration, and nothing automatic is "
        "typed into it."
    )


async def test_permission_failure_is_told_once_by_the_pass() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["timed out", "no Codex daemon"]

    await rig.tick()
    assert rig.told == [], "one failure is retried, not reported"
    for _ in range(4):
        await rig.tick()

    assert rig.told == [(SESSION, "no Codex daemon")]


async def test_permission_failure_seen_while_a_caller_waits_is_told_too() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.server.failures = ["timed out", "timed out"]

    assert (await rig.apply_pass.wait_ready(SESSION, PROVIDER)).unapplied == "timed out"
    await rig.tick()

    assert rig.told == [(SESSION, "timed out")]


async def test_permission_failure_of_an_unbound_session_is_told_with_that_reason() -> None:
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.gave_up.add(str(SESSION))

    await rig.tick()
    await rig.tick()

    assert rig.told == [(SESSION, UNBOUND_REASON)]


async def test_permission_failure_of_a_rollovers_successor_is_left_to_the_rollover() -> None:
    """The rollover ends `permissions-failed` and says so itself: no second notice."""
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.rollover_successors.add(SESSION)
    rig.server.failures = ["timed out", "timed out"]

    assert (await rig.apply_pass.wait_ready(SESSION, PROVIDER)).unapplied == "timed out"
    await rig.tick()

    assert rig.told == []
    assert rig.bindings.states[SESSION].failure == "timed out", "recorded all the same"


async def test_permission_failure_notice_that_raises_costs_only_the_notice() -> None:
    rig = Rig(_record(resumed_from="missing"), _record(SECOND, resumed_from=THREAD))
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    rig.bindings.pending(SECOND, PermissionPreset.ASK)
    rig.notify_error = RuntimeError("the bot is away")

    await rig.tick()
    await rig.tick()

    assert rig.bindings.states[SESSION].failure is not None
    assert rig.bindings.states[SECOND].applied is PermissionPreset.ASK


async def test_permission_failure_from_before_a_service_restart_is_not_told_again() -> None:
    rig = Rig(_record(resumed_from=THREAD))
    rig.bindings.states[SESSION] = PermissionState(PermissionPreset.ASK, None, None, "timed out")

    for _ in range(3):
        await rig.tick()

    assert rig.told == []


async def test_permission_failure_watch_learns_on_its_first_look_then_reports_each_once() -> None:
    """The console is another process and reads the store. A console opened later is not the
    moment to report an old failure, which the bot has said and `doctor` still counts."""
    rig = Rig(_record(), _record(SECOND))
    rig.bindings.states[SESSION] = PermissionState(PermissionPreset.ASK, None, None, "timed out")
    rig.bindings.pending(SECOND, PermissionPreset.ASK)
    watch = PermissionFailureWatch(rig.bindings, rig._sessions)

    assert await watch.newly_failed() == ()

    await rig.bindings.mark_failed(SECOND, "no Codex daemon", requested=PermissionPreset.ASK)
    fresh = await watch.newly_failed()

    assert [(record.session_id, reason) for record, reason in fresh] == [
        (SECOND, "no Codex daemon")
    ]
    assert await watch.newly_failed() == ()


async def test_permission_failure_watch_reports_a_session_that_fails_again_after_it_cleared() -> (
    None
):
    rig = Rig(_record())
    rig.bindings.pending(SESSION, PermissionPreset.ASK)
    watch = PermissionFailureWatch(rig.bindings, rig._sessions)
    assert await watch.newly_failed() == ()
    await rig.bindings.mark_failed(SESSION, "timed out", requested=PermissionPreset.ASK)
    assert len(await watch.newly_failed()) == 1

    await rig.bindings.set_requested(SESSION, PermissionPreset.FULL_ACCESS)
    assert await watch.newly_failed() == ()
    await rig.bindings.mark_failed(SESSION, "timed out", requested=PermissionPreset.FULL_ACCESS)

    assert len(await watch.newly_failed()) == 1


def test_permission_failure_or_success_reads_as_one_inspection_line() -> None:
    ask = PermissionPreset.ASK
    default = PermissionPreset.PROVIDER_DEFAULT

    assert permission_state_line(ask, PermissionState(ask, ask, "d", None)) == "applied"
    assert (
        permission_state_line(ask, PermissionState(ask, None, None, "no Codex daemon"))
        == "not applied: no Codex daemon"
    )
    # A re-apply that failed after a daemon restart: what the pane had is gone.
    assert (
        permission_state_line(ask, PermissionState(ask, ask, "d", "timed out"))
        == "not applied: timed out"
    )
    assert permission_state_line(ask, PermissionState(ask, None, None, None)) == "not applied yet"
    assert permission_state_line(default, PermissionState(default, None, None, None)) == "applied"
    assert (
        permission_state_line(ask, None)
        == "launched with flags (v0.69.0); relaunch for Remote Control"
    )
    # Launched before this version with no flag at all: it is on the daemon already.
    assert permission_state_line(default, None) is None
    # No snapshot either: nothing is known about how it was launched.
    assert permission_state_line(None, None) is None


# --- The pass as `serve` builds it: over the real binding store and the real state rows. ---


class Service:
    """`build_permission_apply_pass` over a real store, with the provider's server faked."""

    def __init__(self, tmp_path) -> None:
        from remote_agents.adapters.agents.registry import provider_descriptors
        from remote_agents.adapters.agents.thread_binding import FileThreadBindings
        from remote_agents.adapters.sqlite.database import open_database
        from remote_agents.adapters.sqlite.migrations import MIGRATIONS
        from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
        from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
        from remote_agents.composition.service import build_permission_apply_pass

        self.spool = tmp_path / "activity"
        self.connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
        self.store = SQLiteSessionStore(self.connection)
        self.state = SQLitePermissionBindings(self.connection)
        self.files = FileThreadBindings(self.spool)
        self.server = FakeServer()
        self.apply_pass = build_permission_apply_pass(
            store=self.store,
            descriptors=provider_descriptors(),
            activity_directory=self.spool,
            connection=self.connection,
        )
        assert list(self.apply_pass._appliers) == ["codex"]
        # The one seam: nothing below `tests/live` reaches the host's own daemon.
        self.apply_pass._appliers["codex"] = self.server

    async def launch(self, preset: PermissionPreset) -> SessionRecord:
        from remote_agents.adapters.agents.hook_identity import (
            RosterEntry,
            roster_path,
            write_roster,
        )

        record = _record(profile=ProfileId("codex"))
        await self.store.save(record, permissions=preset)
        write_roster(
            roster_path(self.spool, "codex"),
            [RosterEntry(str(record.session_id), "/srv/project", record.created_at)],
        )
        self.files.request(str(record.session_id))
        return record

    def bind(self, thread: str = THREAD) -> None:
        from remote_agents.adapters.agents.thread_binding import consume_nonce

        nonce = self.files.mint(str(SESSION))
        assert consume_nonce(self.spool, str(SESSION), nonce, thread) is None


async def test_serve_applies_to_the_thread_the_binding_names_and_only_once_bound(tmp_path) -> None:
    service = Service(tmp_path)
    await service.launch(PermissionPreset.ASK)

    await service.apply_pass.run_once()
    assert service.server.calls == [], "wanted, not yet bound"

    service.bind()
    await service.apply_pass.run_once()
    await service.apply_pass.run_once()

    assert service.server.calls == [(THREAD, PermissionPreset.ASK)]
    state = await service.state.state(SESSION)
    assert (state.applied, state.daemon, state.failure) == (PermissionPreset.ASK, "daemon-A", None)


async def test_serve_does_not_apply_to_a_thread_the_sessions_own_mark_vetoes(tmp_path) -> None:
    """The thread file names the session, but the pane has since reported another thread. The
    binder would not call that bound, so a policy is not sent to it either."""
    from remote_agents.adapters.agents.turn_markers import FileConversationMarks

    service = Service(tmp_path)
    await service.launch(PermissionPreset.FULL_ACCESS)
    service.bind()
    FileConversationMarks(service.spool).record(str(SESSION), "another-thread")
    assert not service.files.names(str(SESSION), "codex"), "the veto is the required control"

    await service.apply_pass.run_once()

    assert service.server.calls == []
    assert (await service.state.state(SESSION)).failure is None


async def test_serve_records_a_binding_that_gave_up_as_a_failure(tmp_path) -> None:
    service = Service(tmp_path)
    await service.launch(PermissionPreset.ASK)
    service.files.give_up(str(SESSION))

    await service.apply_pass.run_once()

    assert service.server.calls == []
    assert (await service.state.state(SESSION)).failure == "its thread could not be bound"


# --- Everything that types on its own waits for the apply (OD-2). ---

UNAPPLIED = Readiness(unapplied="timed out")


def _answers(readiness: Readiness | Exception):
    """A `ready` that answers `readiness`, taking the session and, where given, its profile."""

    async def ready(session_id, profile_id=None):
        if isinstance(readiness, Exception):
            raise readiness
        return readiness

    return ready


async def _drive_rollover(readiness, tmp_path) -> list:
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore

    from .test_rollover_binding import _gate
    from .test_rollover_pass import Rig as RolloverRig

    rig = RolloverRig(SQLiteRolloverStore(open_database(tmp_path / "rollover.sqlite3")))
    _gate(rig, _answers(readiness))
    rig.ready()
    await rig.run(passes=3)
    return rig.sends


async def _drive_schedule(readiness, tmp_path) -> list:
    from .test_schedule_pass import FIRE, MemoryScheduleStore, _run, _schedule
    from .test_schedule_pass import Rig as ScheduleRig

    rig = ScheduleRig(ready=_answers(readiness))
    await _run(MemoryScheduleStore(_schedule()), rig, FIRE)
    return rig.sends


async def _drive_restart_nudge(readiness, tmp_path) -> list:
    from remote_agents.application.interrupted_resume import (
        NUDGING,
        InterruptedResumePass,
        ResumeReport,
    )
    from remote_agents.ports.terminal import PromptDelivery, PromptOutcome

    sends: list = []
    recorded: dict[str, str] = {}

    class Outcomes:
        async def record(self, session: str, outcome: str, *, decided_at) -> bool:
            recorded[session] = outcome
            return True

    async def send(session_id, text):
        sends.append((session_id, text))
        return PromptDelivery(PromptOutcome.SENT)

    async def unused(*_args, **_kwargs):
        raise AssertionError("the nudge step calls nothing else")

    restart = InterruptedResumePass(
        Outcomes(),
        resume=unused,
        folder_of=unused,
        key_claimed=unused,
        send=send,
        ready=_answers(readiness),
        now=lambda: START,
    )
    old = _record(SECOND, state=SessionState.ENDED)
    report = await restart._nudged(
        ResumeReport(old, f"{NUDGING}:{SESSION}", SESSION, True), stopping=False
    )
    assert recorded[str(SECOND)] == report.outcome
    if not sends:
        assert report.outcome == f"resumed:{SESSION}; not nudged:{PERMISSIONS_HELD}"
    return sends


async def _drive_limit_nudge(readiness, tmp_path) -> list:
    from remote_agents.application.limit_resume import LimitResume, NotResumed
    from remote_agents.ports.limit_stop_outcomes import LimitStop
    from remote_agents.ports.terminal import PromptDelivery, PromptOutcome

    sends: list = []

    async def send(session_id, text):
        sends.append((session_id, text))
        return PromptDelivery(PromptOutcome.SENT)

    async def yes(*_args) -> bool:
        return True

    resume = LimitResume(send=send, enabled=yes, settled=yes, amend=yes, ready=_answers(readiness))
    stop = LimitStop(str(SESSION), START, None, START.isoformat())  # type: ignore[arg-type]
    verdict = await resume.nudge(stop)
    if not sends:
        assert verdict.reason is NotResumed.PERMISSIONS
        assert (await resume.nudge(stop)) == verdict, "given up for this stop, not tried again"
    return sends


#: Every application module that is handed a way to type into a pane, and what it is.
#: A driver types through the module's own pass; the others say why they need no gate.
AUTOMATIC_TYPERS = {
    "rollover.py": _drive_rollover,
    "schedules.py": _drive_schedule,
    "interrupted_resume.py": _drive_restart_nudge,
    "limit_resume.py": _drive_limit_nudge,
}
NOT_AUTOMATIC_WORK = {
    "prompt_relay.py": "the owner's own message, relayed when they send it",
    "thread_binding.py": "the binding line, which the apply itself waits on",
    "prompt_delivery.py": "reads what a send came to, and sends nothing",
}


def _modules_that_can_type() -> set[str]:
    import re
    from pathlib import Path

    import remote_agents.application as application

    typing = re.compile(r"\b(PromptDelivery|send_prompt|send_binding_line)\b")
    return {
        path.name
        for path in Path(application.__file__).parent.glob("*.py")
        if typing.search(path.read_text(encoding="utf-8"))
    }


async def test_every_automatic_typer_is_held_until_permissions_are_applied(tmp_path) -> None:
    """Enumerated from the source, so a new module that can type is red until it is listed,
    and a listed one is driven for real: nothing typed while the preset is not applied, or
    while that cannot be told; typed once it is."""
    found = _modules_that_can_type()
    assert found, "the enumeration found nothing, so it proves nothing"
    assert found == set(AUTOMATIC_TYPERS) | set(NOT_AUTOMATIC_WORK), (
        "a module that can type into a pane is neither held behind wait_ready nor explained"
    )

    for index, (module, drive) in enumerate(AUTOMATIC_TYPERS.items()):
        for held in (UNAPPLIED, Readiness(bound=False, unapplied=UNBOUND_REASON), RuntimeError()):
            folder = tmp_path / f"{index}-held-{type(held).__name__}-{getattr(held, 'bound', 0)}"
            folder.mkdir()
            assert await drive(held, folder) == [], f"{module} typed into an unapplied session"
        folder = tmp_path / f"{index}-ready"
        folder.mkdir()
        assert len(await drive(READY, folder)) == 1, f"{module} typed nothing when ready"
