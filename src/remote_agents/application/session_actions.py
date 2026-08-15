"""The single authority over which lifecycle actions a session offers.

Action availability is a property of the lifecycle, not of whichever surface happens to
render it, so it lives here rather than in a driver adapter (DEC-001). Every adapter asks
this module and renders exactly what it returns; none of them branch on state themselves.

This reconciles three adapter-resident copies that had drifted apart: a list builder that
offered `force` from every state including STARTING, a token issuer that refused it unless
the state was one of RUNNING, STOP_REQUESTED, PRESERVED or FAILED, and an executor that
rechecked the live record against a fourth spelling of the same rule.

Availability is a *narrowing* of the domain's legal transitions, never a widening: an action
this module offers must be one `domain.state_machine` will actually perform, or the owner
takes an offered action and receives an exception instead of a stopped session.
`tests/architecture/test_policy_matches_domain.py` enforces that direction.

ORPHANED is deliberately **not** forceable. It is tempting — it is the one state the two
prior copies never agreed on, and it can be stopped from no surface today — but the domain
has no transition out of ORPHANED at all, so a force from it raises InvalidTransition before
the terminal is reached. ORPHANED does not mean "the pane is gone" (that is
RECONCILED_TERMINAL_MISSING, which ends the session); it means the evidence was ambiguous, or
a tmux tag was found with no store row. `reconcile.py` quarantines it for local attention on
purpose. Making it forceable is a real capability with a real safety question — it would put
a one-tap kill on a possibly-live pane this app does not own — and it needs a domain
transition and a recorded decision, not a policy edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from remote_agents.domain.models import ProfileId, SessionState
from remote_agents.domain.trust import TRUST_ANSWERABLE, TrustState

GRACEFUL = "graceful"
CLEANUP = "cleanup"
FORCE = "force"

ACTION_LABELS: dict[str, str] = {
    GRACEFUL: "Stop and close",
    CLEANUP: "Clean up",
    FORCE: "Force stop",
}
"""What each action is called on screen, for every surface that offers one.

An action's name is part of the policy for the same reason its availability is: the owner
learns one vocabulary and meets it everywhere. The bot used to title-case the action id into
"Graceful" while the terminal said "Graceful stop", so the two surfaces disagreed about what
the same button did, and `tests/contract/test_session_actions_parity.py` had to carry a
translation table between them to compare surfaces at all.

`graceful` is labelled by its effect rather than its mechanism. It ends the session and
discards the pane's output — a cost the owner does not get a second chance at — sitting next
to read-only actions, and "Graceful" described how the agent exits rather than what the owner
is about to lose. Naming the effect is the whole of the mitigation here: DEC-018 declined a
confirmation for it on both surfaces, because graceful stop is the ordinary way a session ends
and a confirmation on the common path teaches the owner to dismiss the one guarding force
stop. The lifecycle keeps calling the action `graceful`; only its name on screen changed.
"""

_FORCEABLE = frozenset(
    {
        SessionState.RUNNING,
        SessionState.STOP_REQUESTED,
        SessionState.PRESERVED,
        SessionState.FAILED,
    }
)


def available_actions(state: SessionState) -> tuple[str, ...]:
    """Return the stop actions offerable from `state`, force last.

    STARTING offers nothing: the pane may not exist yet, and the domain has no stop
    transition from it; a stuck STARTING session is resolved by reconciliation instead.
    ENDED and ORPHANED offer nothing: both are read-only in the state machine.
    """
    actions: list[str] = []
    if state is SessionState.RUNNING:
        actions.append(GRACEFUL)
    if state is SessionState.PRESERVED:
        actions.append(CLEANUP)
    if state in _FORCEABLE:
        actions.append(FORCE)
    return tuple(actions)


_EXPLANATIONS = {
    SessionState.STARTING: "The agent is starting; it is not ready to act on yet.",
    SessionState.RUNNING: "The agent is running.",
    SessionState.STOP_REQUESTED: "A graceful stop is in progress.",
    SessionState.PRESERVED: "The agent exited; its output is preserved for inspection.",
    SessionState.FAILED: (
        "The session did not become ready. Its pane may still exist; a later readiness "
        "check can still promote it."
    ),
    SessionState.ENDED: "The session is closed; nothing is left to reach.",
    # Two producers, and the wording has to fit both: a record whose pane evidence was
    # neither live nor preserved (reconcile.py "ambiguous_terminal"), and a trusted pane
    # found with no record at all ("unknown_session"). It is quarantined either way, and
    # nothing the owner does moves it out — there is no transition from ORPHANED.
    SessionState.ORPHANED: (
        "This session and the panes on this host could not be reconciled, so it is held "
        "aside. No action is offered for it."
    ),
}


def explain_state(state: SessionState) -> str:
    """One line describing `state` to the owner, for any surface that renders a session.

    Every member is spelled out rather than defaulted. The bot previously classified four
    states and fell back to "This session is no longer active." for the rest, which was
    false for STARTING — a session that is actively coming up.
    """
    return _EXPLANATIONS[state]


class _RemoteControllable(Protocol):
    """The two fields availability turns on; any session record satisfies this."""

    profile_id: ProfileId
    state: SessionState


def remote_control_available(record: _RemoteControllable) -> bool:
    """Whether a surface should offer the Claude Remote Control toggle for `record`.

    Only Claude implements the pane action, and only a live pane can receive it.

    The two axes are defended unequally, which matters for any surface built on this.
    `SessionService.set_remote_control` re-checks the **profile** independently, so a caller
    that skips this function still cannot drive a non-Claude pane. It does **not** check the
    state: this function is the only thing standing between a caller and a Remote Control
    toggle on a STARTING, STOP_REQUESTED or ORPHANED Claude session. Consult it before
    offering the toggle; do not treat the service as a backstop for the state half.
    """
    return record.profile_id == ProfileId("claude") and record.state is SessionState.RUNNING


def trust_available(record: _RemoteControllable, observed: TrustState) -> bool:
    """Whether a surface should offer to answer the folder-trust question for `record`.

    Availability turns on the **pane**, not the record, which is why the observed state is a
    parameter rather than something this function goes and reads. A record cannot tell you
    whether a dialog is on screen, and the answer stops being true the moment anyone answers
    it -- so a surface consults this with a fresh observation or not at all.

    Deliberately *not* folded into `available_actions`. That function is the stop-action
    policy the parity contract pins (DEC-007), and its answer is a pure function of
    `SessionState`; adding a pane-dependent row to it would make the contract's comparison
    depend on what an agent happened to be printing. This follows `remote_control_available`
    instead, which is the established shape for an action that is not a stop.

    Unlike Remote Control, no session state is required. The state a trust-blocked launch
    lands in is FAILED -- the readiness marker never arrived -- so gating on RUNNING would
    refuse the one case this exists for. `TmuxRuntime.answer_trust` re-reads the pane before
    sending anything, so a surface that offers this on a stale observation still cannot fire
    a keypress into a session that is no longer asking.
    """
    return record.profile_id in TRUST_ANSWERABLE and observed is TrustState.AWAITING


UNKNOWN_SESSION = "unknown_session"
GRACEFUL_TIMEOUT = "graceful_timeout"
OWNERSHIP_LOST = "ownership_lost"


@dataclass(frozen=True, slots=True)
class StopFailure:
    """Why a graceful stop did not take effect, in the one vocabulary both surfaces use.

    Two fields rather than one string because the two halves are read in different places and
    at different lengths. `summary` is one short sentence, so it fits a one-line region — the
    local surface's status line is literally one line high — and `remedy` is what the owner
    does about it, which is longer than any of them and belongs wherever that surface puts an
    explanation. A single blob would have forced one of the two surfaces to cut it.

    Both are whole sentences rather than fragments, so a surface can put either one first
    without case surgery. The first version made `summary` a fragment ("the stop was never
    sent"), which read correctly in the middle of the status line and needed capitalising to
    open a notification — a transformation each surface would have written for itself, which
    is the first step back towards two vocabularies.
    """

    detail: str
    summary: str
    remedy: str


class _StopObservation(Protocol):
    """The two fields a stop's outcome turns on; any terminal observation satisfies this."""

    preserved: bool
    detail: str


#: The causes a graceful stop can report without preserving the pane, and what each one means
#: to the owner. Keyed by the `detail` the terminal adapter sets, which is the only place the
#: two are distinguished — the observation is otherwise identical.
_STOP_FAILURES: dict[str, tuple[str, str]] = {
    UNKNOWN_SESSION: (
        "The stop was never sent.",
        "Nothing was signalled to the agent and nothing was stopped, because this host could "
        "not match the session to a live pane it owns: either no profile is curated here for "
        "that agent, or no managed pane was found for it, or the pane found belongs to a "
        "different one. Whichever it is, it is this host's view of the session and not "
        "something the agent did. Run `doctor --profiles`, check the managed sessions on this "
        "host, or force stop the session to end it now.",
    ),
    GRACEFUL_TIMEOUT: (
        "The agent did not exit in time.",
        "The exit sequence was sent and no clean exit was seen before the wait ran out, so "
        "nothing was recorded as stopped and nothing was removed. That is about the agent "
        "rather than this host's view of the session. Try again if it may still be finishing, "
        "or force stop it.",
    ),
    OWNERSHIP_LOST: (
        "This host had no pane left to stop.",
        "The session is no longer in this host's managed inventory, so nothing was killed: it "
        "was destroyed outside this app, or its ownership metadata drifted. The record has "
        "been cleared either way, so the session will not come back to the list. If an agent "
        "may still be running behind it, look for it with "
        "`tmux -L remote-agents list-panes -a` and end it there.",
    ),
}
"""Deliberately worded so no two of them can be mistaken for each other.

`unknown_session` names a **disjunction**, and an earlier version of it did not. `TmuxRuntime.
graceful_stop` returns that one detail for three conditions — no curated profile, no managed
pane found, and a pane whose profile differs — and the wording asserted the first of them as
though it were the only one, sending an operator to `doctor --profiles` for a problem that
might not be there. A gate evaluator caught it. What is true in all three, and what the summary
therefore says, is that nothing was signalled: the surface never reached the agent at all.

`graceful_timeout` was narrowed for the same reason and by the second gate pass. It said "the
agent was still running when the wait ran out" — but `TmuxRuntime.graceful_stop`'s poll loop
treats an `inspect()` of `None` exactly like "not preserved yet", so a pane destroyed mid-wait
by a server restart or the other writer lands here too, with `live=True` and an agent that is
not running at all. What is true in every case is that no clean exit was *seen*, which is what
it now says. Neither of these paragraphs is a softening: both causes still name what happened
and what to do, and they still cannot be mistaken for one another.

They are a *configuration* problem and an *agent-behaviour* problem, and the owner's next
step differs completely: one is fixed on this host, the other is waited out or forced. BL-008
was that neither was reported at all — both surfaces discarded `graceful_stop`'s return value
— and reporting them in words that read alike would close the entry without answering it.

`unknown_session` exists because of DEC-006: a stop fails closed on an unresolved profile
rather than guessing at one. This makes that refusal legible; it does not soften it.

`ownership_lost` is the third entry and the odd one, because it is the only cause here that
does **not** describe a stop that left the session where it was. It belongs to *force*, and
under DEC-017 force keeps clearing the record even when it finds no pane — so the session does
end, and the row does go away. What was wrong was the claim: both surfaces reported "Force
stopped X" over a kill nobody observed. It shares this table rather than growing a second one
because DEC-007's whole argument is that the two surfaces speak one vocabulary, and a cause
with its own private wording on each surface is how that stops being true.

Read it with DEC-017's accepted cost 1 in hand: `VERIFIED_FORCE_STOP` is still written to the
durable history whether or not `kill-session` ran, so the audit log cannot tell these apart and
only this sentence carries the distinction. That asymmetry with DEC-006 — graceful fails closed
on an unresolved profile, force does not fail closed on an unresolved pane — is recorded and
deliberate, not an oversight to be "restored".
"""


def stop_failure(observation: _StopObservation) -> StopFailure | None:
    """Why the stop did not take effect, or `None` when it did.

    **An unrecognised `detail` is a failure, not an unknown.** `preserved` is false means the
    profile's exit sequence did not work, whatever the terminal called the reason, so falling
    through to `None` there would report a stop that did nothing as a stop that succeeded —
    the same fail-dangerous default `_issue_stop` removed from its own dispatch. The generic
    wording names the raw detail so a cause nobody has a sentence for is still traceable.
    """
    if observation.preserved:
        return None
    known = _STOP_FAILURES.get(observation.detail)
    if known is None:
        return StopFailure(
            observation.detail,
            "The terminal did not report a clean exit.",
            f"The terminal reported {observation.detail!r} and the session was left as it is. "
            "Force stop it if you need it ended now.",
        )
    summary, remedy = known
    return StopFailure(observation.detail, summary, remedy)


def force_stop_failure(observation: _StopObservation) -> StopFailure | None:
    """What force stop actually observed, or `None` when it killed the pane it was asked to.

    **Force cannot be read by `stop_failure`, and the reason is worth stating rather than
    discovering.** That function keys on `preserved`, because for a graceful stop a preserved
    pane *is* the success. Force removes the pane, so `preserved` is false on every outcome
    including the good one — handing a force observation to `stop_failure` would report every
    successful kill as a failure. The discriminator here is the detail alone.

    So this is deliberately the mirror image of its sibling's fail-closed default: an
    unrecognised detail answers `None`, meaning "nothing to report". `TmuxRuntime.force_stop`
    sets a detail only for `ownership_lost`; the ordinary kill carries none. Defaulting the
    unknown to a failure here would announce one on every successful force the moment a future
    detail is added for some unrelated reason, which is the louder wrong answer.

    What this does **not** do is change the outcome. DEC-017 keeps `SessionService.force_stop`
    recording `VERIFIED_FORCE_STOP` and the record reaching ENDED either way, because a row the
    owner cannot clear is a worse failure than an over-confident message. This is the message.
    """
    known = _STOP_FAILURES.get(observation.detail)
    if known is None:
        return None
    summary, remedy = known
    return StopFailure(observation.detail, summary, remedy)
