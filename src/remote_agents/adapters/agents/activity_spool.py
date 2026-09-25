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

from remote_agents.adapters.agents.turn_markers import FileTurnMarkers
from remote_agents.ports.agent_activity import bounded_detail_line
from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE, safe_session_id
from remote_agents.ports.turn_markers import TurnMarkers

MAXIMUM_PAYLOAD_BYTES = 32_768

#: The event that starts a turn, and the events that end one (BL-108, DEC-104). Read from the
#: payload's `hook_event_name`, beside the agent's own `session_id` that the marker keeps as its
#: owner: a submit's payload also carries the owner's `prompt`, and nothing here reads it.
#: Claude and Codex both spell `UserPromptSubmit` and `Stop` this way; only
#: Claude has a `StopFailure` (Codex fires nothing on a failed turn, `ports/agent_activity.py`).
TURN_STARTED = "UserPromptSubmit"
TURN_ENDED = frozenset({"Stop", "StopFailure"})
#: The providers whose hook marks a turn: the two whose `Stop` also ends one. Not OpenCode: its
#: plugin forwards only its own two events, and its "finished" is `session.idle`, which is not in
#: `TURN_ENDED` -- a marker started there would never end. A provider added later starts none
#: until someone decides it should.
MARKED_PROVIDERS = frozenset({"claude", "codex"})

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
#: `Stop` is the only key here. `PermissionRequest`'s detail is composed separately, by
#: `_ask_detail`, because its words live one level down inside `tool_input` rather than at the
#: top level this mapping is read with.
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
#: `detail`, but into `ask`, a field of its own (`_CODEX_ASK_FIELDS`, DEC-074). This paragraph is
#: kept because its argument is why the two fields are separate; it is annotated because a reader
#: hits it a hundred lines before the code that admits the field, and would otherwise leave with
#: the wrong conclusion.
#:
#: **Amended 2026-09-19 by DEC-098, and this is the paragraph a reader must not stop at.** The
#: argument above concluded that a bare provider token is not prose and so does not belong in
#: `detail`. That conclusion stands and is why `ask` still exists. What DEC-098 reverses is the
#: separate refusal of `tool_input`: the owner is the sole operator and the sole recipient, so
#: their own command is not a leak, and `tool_input.description` is not a token at all -- it is
#: the agent's own one-sentence reason, which is precisely what `detail` has always meant. The
#: words now ride in `detail` beside the class in `ask`; neither field took the other's job.
_CODEX_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {"Stop": ("last_assistant_message",)}

#: What a Codex payload may contribute as an ASK CLASS, per event. `PermissionRequest` only,
#: and `tool_name` only -- the one field the measurement
#: (`docs/acceptance-2026-08-29-codex-activity-detail.md`) licenses, whose own licensing
#: section reads "`PermissionRequest` -> `tool_name` at most, and nothing else" -- a licence
#: **amended on 2026-09-19 by DEC-098**, which additionally admits `tool_input`'s two measured
#: keys into `detail`; this tuple, which is about `ask` alone, is unchanged by that. `Stop` admits
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
    markers: TurnMarkers | None = None,
) -> int:
    """Record one hook event privately, and always report success to the agent.

    A submit starts the session's turn marker and records nothing else; a finished turn ends
    the marker and is then recorded as before.
    """
    try:
        session_id = safe_session_id(environment.get(SESSION_ID_VARIABLE))
        if session_id is None:
            return 0
        raw = payload.read(MAXIMUM_PAYLOAD_BYTES + 1)
        document = _document(raw)
        if document is not None:
            fields: Mapping[str, object] = document
        elif len(raw) > MAXIMUM_PAYLOAD_BYTES:
            # Past the bound nothing is recorded, as before; but a long pasted prompt is still a
            # turn, and a long final answer still ends one, so the two fields the marker needs
            # are recovered from the prefix already read.
            fields = _fields_in_prefix(raw, _MARKER_FIELDS)
        else:
            return 0
        event = fields.get("hook_event_name")
        # The agent's own id for its session, not the pane's: an agent started inside a managed
        # pane inherits the pane's id, and only this tells its hooks from its parent's.
        owner = fields.get("session_id")
        turns = FileTurnMarkers(activity_directory) if markers is None else markers
        if event == TURN_STARTED and provider in MARKED_PROVIDERS:
            turns.start(session_id, owner=owner if isinstance(owner, str) else None)
            return 0
        # Ungated, unlike the start: ending a marker that was never started is a no-op. Guarded
        # on its own, so a marker that cannot be removed never costs the "finished" record.
        if event in TURN_ENDED:
            try:
                turns.end_if_owned_by(session_id, owner)
            except Exception:
                pass
        if document is None:
            return 0
        observed = _observed(document, session_id, now(), provider)
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
    document = _payload_document(payload)
    return None if document is None else _observed(document, session_id, moment, provider)


def _payload_document(payload: IO[bytes]) -> dict | None:
    """The payload as a JSON object, read once and bounded, or None."""
    return _document(payload.read(MAXIMUM_PAYLOAD_BYTES + 1))


def _document(raw: bytes) -> dict | None:
    """A bounded read as a JSON object, or None -- also when it filled the bound."""
    if not raw or len(raw) > MAXIMUM_PAYLOAD_BYTES:
        return None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


_DECODER = json.JSONDecoder()
_BLANK = re.compile(r"[ \t\n\r]*")
#: What the marker step reads from a payload: the event, and the agent's own id for its session.
_MARKER_FIELDS = ("hook_event_name", "session_id")


def _fields_in_prefix(raw: bytes, names: tuple[str, ...]) -> dict[str, object]:
    """The named top-level fields of a payload cut at the bound, as far as the cut allows.

    Walks the object's top-level keys in order, decoding each value whole with the standard
    decoder and discarding the ones not named, so text inside a value -- a prompt that spells a
    key -- is never read as a key. The walk stops at the first value the cut leaves incomplete:
    a field that comes after the long one is not in the prefix, and is not guessed. Claude
    2.1.282 and Codex 0.155.1 both send `session_id` and `hook_event_name` before `prompt` and
    `last_assistant_message` (measured 2026-09-25).
    """
    found: dict[str, object] = {}
    text = raw.decode("utf-8", errors="replace")
    try:
        position = _BLANK.match(text, 0).end()
        if text[position] != "{":
            return found
        position += 1
        while True:
            position = _BLANK.match(text, position).end()
            if text[position] != '"':
                return found
            key, position = _DECODER.raw_decode(text, position)
            position = _BLANK.match(text, position).end()
            if text[position] != ":":
                return found
            position = _BLANK.match(text, position + 1).end()
            value, position = _DECODER.raw_decode(text, position)
            if key in names:
                found[key] = value
                if len(found) == len(names):
                    return found
            position = _BLANK.match(text, position).end()
            if text[position] != ",":
                return found
            position += 1
    except (IndexError, ValueError):
        return found


def _observed(
    document: dict, session_id: str, moment: datetime, provider: str = "claude"
) -> ObservedAgentEvent | None:
    """Keep only the fields a notification is built from."""
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
        # `PermissionRequest` admits `tool_name` as an `ask` (DEC-074), and since 2026-09-19
        # admits its `tool_input` words as `detail` too (DEC-098) -- two fields for two kinds of
        # string, which is DEC-067's field-conflation reasoning still doing its job rather than
        # being overturned by it. What DEC-098 reversed was DEC-067's separate refusal of
        # `tool_input` as a leak; see `_ask_detail`.
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
            detail=(
                _ask_detail(document)
                if event == "PermissionRequest"
                else _first(document, _CODEX_DETAIL_FIELDS.get(event, ()), bounded_detail_line)
            ),
            observed_at=moment.astimezone(UTC),
            ask=_first(document, _CODEX_ASK_FIELDS.get(event, ()), _plain_token),
        )
    # `PermissionRequest` is Claude's since 2026-09-19 (DEC-098) and is read exactly as Codex's
    # is: the tool class into `ask`, through the narrow token reader, and the ask's own words
    # into `detail` through `_ask_detail`. `_DETAIL_FIELDS` is not consulted for it -- a
    # `PermissionRequest` carries no `message` and no `last_assistant_message`, and reading a
    # group of fields that cannot be present would only obscure which one was expected.
    if event == "PermissionRequest":
        return ObservedAgentEvent(
            session_id=session_id,
            event=event,
            reason=None,
            detail=_ask_detail(document),
            observed_at=moment.astimezone(UTC),
            ask=_plain_token(document.get("tool_name")),
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


#: What a `PermissionRequest`'s nested `tool_input` may contribute as DETAIL (DEC-098).
#:
#: Two keys, measured on both providers the same day
#: (`docs/acceptance-2026-09-19-ask-payloads.md`): Codex `Bash` and Claude `Bash` each carry
#: `command` and `description`, and Codex `apply_patch` carries `command` alone. That the two
#: agents agree on these names is why one reader serves both rather than one per provider.
#:
#: **Read by name, one level down, and no further.** `tool_input` is the only nested object this
#: spool descends into, and it descends exactly one level: a recursive walk would turn every
#: future tool's payload into detail sight unseen, which is the assumption
#: `_DISCRIMINATING_FIELDS` exists to warn about. A tool carrying neither key yields no detail
#: and still spools its record.
#:
#: Deliberately NOT admitted, and still refused after DEC-098: `transcript_path` and `cwd`, which
#: are on this event and are filesystem layout rather than anything the owner is being asked
#: about.
_ASK_REASON_FIELD = "description"
_ASK_COMMAND_FIELD = "command"

#: What a Claude ask carries when it is not a command: a path, or a question.
#:
#: Measured the same day. `Edit` carries `file_path` beside `old_string`/`new_string`, and those
#: two are file CONTENT -- deliberately not read, because the owner is being asked *which file*,
#: not shown a diff on a phone. `AskUserQuestion` nests its text one level deeper again, in
#: `questions[0]["question"]`, and only the question is taken: the options are a menu, and a
#: menu is what opening the session is for.
_ASK_PATH_FIELD = "file_path"
_ASK_QUESTIONS_FIELD = "questions"
_ASK_QUESTION_FIELD = "question"


def _first_question(tool_input: Mapping[str, object]) -> str | None:
    """The text of the first question an `AskUserQuestion` payload carries, if it carries one."""
    questions = tool_input.get(_ASK_QUESTIONS_FIELD)
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0]
    if not isinstance(first, Mapping):
        return None
    return bounded_detail_line(first.get(_ASK_QUESTION_FIELD))


def _ask_detail(document: Mapping[str, object]) -> str | None:
    """Compose the agent's reason and the command it is about into one bounded line.

    Either half alone when the other is absent -- which is not defensive coding but the measured
    case: `apply_patch` carries a command and no description, and a formatter written from the
    `Bash` sample alone would render the literal word `None` into a notification.

    Each half is bounded before it is joined and the join is bounded again. The inner pass is
    what keeps a 30 KB patch envelope from being concatenated in full before being cut, and the
    outer pass is what keeps the budget the far end measures against honest once a separator has
    been added between them.
    """
    tool_input = document.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return None
    reason = bounded_detail_line(tool_input.get(_ASK_REASON_FIELD))
    command = bounded_detail_line(tool_input.get(_ASK_COMMAND_FIELD))
    if reason is not None and command is not None:
        return bounded_detail_line(f"{reason} — $ {command}")
    if command is not None:
        return bounded_detail_line(f"$ {command}")
    if reason is not None:
        return reason
    # Ordered by how much the half above says, not by provider. A tool carrying a command is
    # answered by the command; one carrying only a path is answered by the path; a question is
    # answered by itself. Nothing here branches on `tool_name`, so a tool that starts carrying
    # a `description` gains one without this function learning its name.
    path = bounded_detail_line(tool_input.get(_ASK_PATH_FIELD))
    if path is not None:
        return path
    return _first_question(tool_input)


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
