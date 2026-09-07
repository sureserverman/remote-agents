"""Spool what an agent hook observed into the service's private activity directory.

This runs as a hook inside the agent's own process, not inside the service, which fixes two
things about it. The first is that it must never fail loudly: anything raised here surfaces in
the session the operator is working in, so losing one activity record is always the lesser
failure and every path below ends in exit status zero. The second is that it is reachable by
any agent on the machine, so the environment variable the service exports decides whether to
spool at all: absent or malformed, this writes nothing. That is a guarantee about a session
that merely *doesn't have* the variable - the ordinary case of an operator running claude by
hand - and it is worth stating exactly that narrowly. It is not a guarantee against a process
that sets the variable deliberately, and no check here could be: the spool is owner-only, the
hook runs as that owner, and anything else running as that owner can write into the directory
without going through this file at all.

What the guard below does buy, which the variable cannot, is that an *authorized* record
lands where it was meant to. Deciding who may spool says nothing about where the spool goes,
so the directory is opened through a check that refuses a symlink left lying in wait rather
than through a plain mkdir, which would follow one.

What lands here is deliberately narrower than what the hook receives. The notification the
service will send needs an event name, the field that discriminates that event, one short
line of detail, the class of thing an agent is waiting on, a session, and a time; the
transcript path and working directory the payload also carries would leak filesystem layout
into a Telegram message, so they never leave here.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from remote_agents.ports.agent_activity import bounded_detail_line
from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE, safe_session_id

MAXIMUM_PAYLOAD_BYTES = 32_768

_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: The field each event discriminates on, as the installed agent actually spells them.
#:
#: Measured against `~/.local/share/claude/versions/2.1.227`, not assumed:
#: `StopFailure` carries `error`, `Notification` carries `notification_type`, `SessionEnd`
#: carries `reason`. Two of these were previously `error_type` and `end_reason`, which made
#: `limit_reached` an unreachable kind: a managed session stopping on a rate limit spooled a
#: record whose reason was `None`, and the drain dropped it as an event it could not
#: interpret. Silently, and for the one thing a phone notification is most wanted for.
#:
#: `end_reason` appears nowhere in that bundle. `error_type` appears 58 times and **never in a
#: hook payload** -- it is a telemetry key. The distinction is kept because the first draft of
#: this comment claimed both were absent, which was a `grep -c` on a binary file reporting
#: nothing and being read as zero. The conclusion is unchanged; the evidence for it was
#: overstated, and a comment that overstates its evidence is the failure this whole repair is
#: about.
#:
#: Nothing caught it because both sides were tested against each other: the spool's fixture
#: asserted `error_type` and the classifier's fixture wrote `reason="rate_limit"` directly, so
#: the two halves agreed with each other and neither was ever compared with the agent.
#: `tests/live/test_agent_activity_hooks.py` is where that comparison now lives.
#: `reason` is `SessionEnd`'s, and `SessionEnd` is retired (DEC-051). It stays because a host
#: that has not re-run `install-agent-hooks` yet is still firing that hook at this spool, and a
#: reader that stopped recognising the field would mis-parse those records rather than ignore
#: them. The mapping drops the event either way; what this keeps is the ability to read it
#: correctly on the way to being dropped.
_DISCRIMINATING_FIELDS = ("error", "notification_type", "reason")
_DETAIL_FIELDS = ("message", "last_assistant_message")

#: What a Codex payload may contribute, per event. **Measured, never assumed** --
#: `docs/acceptance-2026-08-29-codex-activity-detail.md` records the field vocabulary of real
#: payloads captured against a disposable `CODEX_HOME`, and this tuple is its licensing section
#: written as code. Deliberately narrower than `_DETAIL_FIELDS`: `message` was never observed on
#: a Codex payload, and a field this project has not seen is not a field it reads.
#:
#: `Stop` is the only key. `PermissionRequest` admits nothing, which is narrower than the
#: measurement permits: `tool_name` is the one field on that event that names the ask without
#: carrying a command, a path or a prompt, and it is declined anyway.
#:
#: **Not because nothing would render it.** That was the first reason given and it was wrong --
#: it holds for `reason`, which only ever feeds `_kind`, and not for `detail`, which is
#: provider-agnostic and renders on both surfaces with no renderer change. The Stage 2 gate
#: evaluator checked rather than believed it.
#:
#: The reason that survives: `detail` means *the agent's own words*. It is what `_detail_of`
#: guards, what the feed elides and expands, and what every consumer reads as a sentence the
#: agent chose to write. A bare provider token is a different kind of string, and the honest
#: version of the owner's ask is a sentence -- "waiting for an answer about a shell command" --
#: which is wording, shared with Claude's `needs_answer`, and a decision to take deliberately
#: rather than to inherit from a parser change. Recorded as DEC-067.
#:
#: **That decision was taken on 2026-09-06 and `tool_name` is now admitted** -- not here, into
#: `detail`, which is what DEC-067 refused and still refuses, but into `ask`, a field of its
#: own (`_CODEX_ASK_FIELDS`, DEC-074). This paragraph is kept because its argument is why the
#: two fields are separate; it is annotated because a reader hits it a hundred lines before the
#: code that admits the field, and would otherwise leave with the wrong conclusion.
_CODEX_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {"Stop": ("last_assistant_message",)}

#: What a Codex payload may contribute as an ASK CLASS, per event. `PermissionRequest` only,
#: and `tool_name` only -- the one field the measurement
#: (`docs/acceptance-2026-08-29-codex-activity-detail.md`) licenses, whose own licensing
#: section reads "`PermissionRequest` -> `tool_name` at most, and nothing else". `Stop` admits
#: none: an agent that has finished is not waiting on anything.
_CODEX_ASK_FIELDS: dict[str, tuple[str, ...]] = {"PermissionRequest": ("tool_name",)}

#: The two `event` types OpenCode's generated plugin acts on, and the only ones this branch
#: admits. Read as an exact set rather than through `_plain_token`, because these names carry a
#: dot and `_plain_token` -- correctly, for the values it guards -- does not allow one. Widening
#: that reader to admit a dotted event name would have loosened the guard on `reason` and on
#: every provider's `ask` at the same time, to make one branch's event names fit.
_OPENCODE_EVENTS = frozenset({"session.idle", "permission.asked"})

#: What an OpenCode payload may contribute as DETAIL: **nothing, permanently**.
#:
#: Empty rather than absent, and empty for a reason that will not change with a wider parser.
#: `session.idle` -- the only event that could carry a finished agent's words -- has a payload of
#: exactly one field, and that field is OpenCode's own session id
#: (`docs/acceptance-2026-09-06-opencode-activity.md`, 2 of 2 samples). There is no
#: `last_assistant_message` waiting to be admitted the way Codex's `Stop` had one; getting the
#: agent's last words would mean asking the SDK for the session's messages, which is a different
#: mechanism reading conversation content and therefore a DEC-013 retention decision rather than
#: a parser widening. Not proposed, and this dict is where a future reader is told so.
_OPENCODE_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {}

#: What an OpenCode payload may contribute as an ASK CLASS. `permission.asked` only, and
#: `properties.permission` only -- the one field the measurement licenses, whose licensing
#: section reads "`permission.asked` -> `properties.permission` at most, as an ask class". Never
#: `patterns` or `metadata.command`, which are the literal command, nor `always`, which is a glob
#: over commands. `session.idle` admits none: an agent that has finished is not waiting.
#:
#: Read through `_plain_token` exactly as Codex's `tool_name` is, and for the same measured
#: reason: one sample, one value (`bash`), so the value space is unverified and a token carrying
#: a space, a slash or a quote is not a tool class this project recognises.
_OPENCODE_ASK_FIELDS: dict[str, tuple[str, ...]] = {"permission.asked": ("permission",)}
#: How many times a colliding name is stepped over before the record is dropped in silence.
#:
#: A collision needs two events in the same *microsecond* for one session, so the hooks
#: firing together at the end of a turn do not approach this; reaching eight means something
#: is wrong that a ninth attempt would not fix. Exhausting it is a silent drop, which is the
#: right answer in a hook -- it is stated here because the loop's `return` is inside the
#: `try`, so the fall-through is easy to read as unreachable rather than as a decision.
_MAXIMUM_NAME_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class ObservedAgentEvent:
    """The bounded shape of one hook observation, and the only shape that reaches disk."""

    session_id: str
    event: str
    reason: str | None
    detail: str | None
    observed_at: datetime
    ask: str | None = None
    """Which CLASS of thing the agent is waiting on, as the provider's own token.

    A third kind of string, kept apart from the other two on purpose. `reason` discriminates an
    event into a kind and is never rendered; `detail` is **the agent's own words** and is
    rendered as a sentence the agent wrote. This is neither: `Bash` is a provider's name for a
    tool, and its whole use is that a *surface* turns it into wording of its own ("waiting for
    an answer about a shell command").

    **DEC-074**, which supersedes DEC-067's rejected-alternative clause. DEC-067 declined
    `tool_name` on two grounds. The first -- that putting a token in `detail` conflates two
    kinds of string in a field every consumer reads as prose -- is answered by this field
    existing, and stands unamended. The second was the **ordering**: "storing ahead of
    rendering inverts the rule above", declined because nobody had yet taken the wording
    decision rendering it would require. That premise is what changed; DEC-074 records the
    decision and the argument for it.

    An earlier version of this docstring claimed the split "completes DEC-067 on its own terms
    and supersedes nothing", and asserted the owner's approval inline. Both were wrong in the
    same way: DEC-067's ordering objection is a separate clause that this does override, and an
    owner decision living only in a code comment is exactly the unrecorded decision this project
    treats as reversible by the next edit. A Tier-1 review found it.
    """

    def document(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "event": self.event,
            "reason": self.reason,
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat(),
            "ask": self.ask,
        }


def spool_agent_event(
    payload: IO[bytes],
    *,
    activity_directory: Path,
    environment: Mapping[str, str] = os.environ,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    provider: str = "claude",
) -> int:
    """Record one hook event privately, and always report success to the agent."""
    try:
        session_id = safe_session_id(environment.get(SESSION_ID_VARIABLE))
        if session_id is None:
            return 0
        observed = _observed_event(payload, session_id, now(), provider)
        if observed is not None:
            _write_privately(observed, activity_directory)
    except Exception:
        # Catching broadly is correct exactly here and nowhere else in this package. This
        # frame is the boundary of a hook running inside the agent's process, so an escaping
        # exception would disrupt the session the operator is working in. Every unexpected
        # failure - an unreadable stream, a spool that is not a writable directory, a full
        # disk - costs one activity record and nothing more.
        return 0
    return 0


def _observed_event(
    payload: IO[bytes], session_id: str, moment: datetime, provider: str = "claude"
) -> ObservedAgentEvent | None:
    """Read a bounded payload and keep only the fields a notification is built from."""
    raw = payload.read(MAXIMUM_PAYLOAD_BYTES + 1)
    if not raw or len(raw) > MAXIMUM_PAYLOAD_BYTES:
        return None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if provider == "opencode":
        return _observed_opencode_event(document, session_id, moment)
    event = _plain_token(document.get("hook_event_name"))
    if event is None:
        return None
    if provider == "codex":
        if event not in {"Stop", "PermissionRequest"}:
            return None
        # Widened on 2026-08-30 from "every payload field discarded" to "the one measured field
        # a notification renders" -- which supersedes nothing. DEC-013 clause (2) already allows
        # a hook to keep "one bounded single line of detail" beside the event name, session id
        # and time, and DEC-063 kept that clause binding while replacing only DEC-013's obsolete
        # claim that Codex has no usable source. Codex simply was not using an allowance Claude
        # has had since the spool was written; this brings it to parity. DEC-063's content-free
        # claim is scoped to the pane-*title* watcher, which is untouched and still retains one
        # boolean.
        #
        # `PermissionRequest` still admits no *detail* -- see `_CODEX_DETAIL_FIELDS` -- and
        # since 2026-09-06 admits `tool_name` as an `ask`, a different field for a different
        # kind of string (DEC-074, superseding DEC-067's rejected-alternative clause;
        # DEC-067's field-conflation reasoning stands and is why `ask` is not `detail`).
        #
        # Read through `_plain_token`, not `bounded_detail_line`. The measurement observed
        # `tool_name` only as `Bash` in all four samples and says so; its value space is
        # unverified beyond that. The narrow reader is what keeps an unverified space from
        # becoming a rendering surface: a value carrying a space, a slash or a quote is not a
        # tool class this project recognises, and it is dropped rather than drawn under the
        # owner's session name.
        #
        # What crosses here is bounded by `bounded_detail_line`, exactly as Claude's is, because
        # the far end of the spool measures against the same budget.
        return ObservedAgentEvent(
            session_id=session_id,
            event=event,
            reason=None,
            detail=_first(document, _CODEX_DETAIL_FIELDS.get(event, ()), bounded_detail_line),
            observed_at=moment.astimezone(UTC),
            ask=_first(document, _CODEX_ASK_FIELDS.get(event, ()), _plain_token),
        )
    return ObservedAgentEvent(
        session_id=session_id,
        event=event,
        reason=_first(document, _DISCRIMINATING_FIELDS, _plain_token),
        detail=_first(document, _DETAIL_FIELDS, bounded_detail_line),
        observed_at=moment.astimezone(UTC),
    )


def _observed_opencode_event(
    document: Mapping[str, object], session_id: str, moment: datetime
) -> ObservedAgentEvent | None:
    """Read an OpenCode plugin record, admitting the two events and the one licensed field.

    Branched ahead of the shared `_plain_token` read rather than after it, which is the whole
    reason this is a function and not three more lines in `_observed_event`. OpenCode's event
    names are dotted; `_plain_token` allows no dot, and it is the same reader that guards
    `reason` and every provider's `ask`. Making the dotted names fit by widening it would have
    loosened two guards to admit one branch's vocabulary -- so the branch takes its event names
    from a fixed set instead, which is narrower than `_plain_token` rather than wider.

    The narrowing here duplicates the plugin's own, deliberately. That code lives in the
    operator's configuration directory where a hand-edit is possible, and this end of the spool
    reads a file a different process wrote; a boundary that trusts what it is handed because
    something upstream was careful is not a boundary.
    """
    event = document.get("hook_event_name")
    if not isinstance(event, str) or event not in _OPENCODE_EVENTS:
        return None
    return ObservedAgentEvent(
        session_id=session_id,
        event=event,
        reason=None,
        detail=_first(document, _OPENCODE_DETAIL_FIELDS.get(event, ()), bounded_detail_line),
        observed_at=moment.astimezone(UTC),
        ask=_first(document, _OPENCODE_ASK_FIELDS.get(event, ()), _plain_token),
    )


def _first(
    document: Mapping[str, object], fields: tuple[str, ...], read: Callable[[object], str | None]
) -> str | None:
    """Return the first field of a group this event actually carries.

    The four hook events name their discriminating field differently, and only one of those
    names is ever present, so reading them as a group avoids branching on the event name and
    keeps an event added upstream from silently losing its detail line.
    """
    values = (read(document.get(field)) for field in fields)
    return next((value for value in values if value is not None), None)


def _plain_token(value: object) -> str | None:
    """Accept an enumerated hook value only in the unpunctuated form the documentation uses."""
    return value if isinstance(value, str) and _PLAIN_TOKEN.fullmatch(value) else None


def _write_privately(observed: ObservedAgentEvent, activity_directory: Path) -> None:
    """Publish one owner-only file, named so the drain can order what it finds.

    The record is written to a uniquely named temporary and then *linked* into place, so it
    appears under the name the drain collects only once all of its bytes are there. Creating
    it directly at its final name left it visible and empty for as long as the write took,
    and a drain passing through that window would have read nothing parseable and deleted it
    -- losing a record the hook had already reported writing.

    ``os.link`` rather than ``os.replace`` because the final name still has to be *claimed*,
    not overwritten: two events in the same microsecond propose the same name, and link fails
    where replace would silently discard the first. Renaming a temporary into place got the
    atomicity right and lost that, since the temporary was gone by the time the second event
    looked for it. ``mkstemp`` opens the temporary owner-only, so the mode is never repaired
    after the fact and the content is never briefly readable by anyone else.
    """
    if open_private_directory(activity_directory) is None:
        return
    content = json.dumps(observed.document(), sort_keys=True).encode("utf-8")
    stamp = observed.observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    descriptor, name = tempfile.mkstemp(dir=activity_directory, prefix=".pending-", suffix=".tmp")
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(_MAXIMUM_NAME_ATTEMPTS):
            suffix = "" if attempt == 0 else f"-{attempt}"
            try:
                os.link(pending, activity_directory / f"{observed.session_id}-{stamp}{suffix}.json")
            except FileExistsError:
                continue
            return
    finally:
        pending.unlink(missing_ok=True)
