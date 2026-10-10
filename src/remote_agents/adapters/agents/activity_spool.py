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
from remote_agents.ports.provider_descriptor import HookLicence
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
_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
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
    markers: TurnMarkers | None = None,
    licence: HookLicence | None = None,
) -> int:
    """Record one hook event privately, and always report success to the agent.

    A submit starts the session's turn marker and records nothing else; a finished turn ends
    the marker and is then recorded as before. What is recorded, and whether a submit marks a
    turn at all, is the `licence` the hook's vertical declares (BL-050): this module names no
    provider, and a hook with no licence records nothing and starts no marker.
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
        if event == TURN_STARTED and licence is not None and licence.starts_turn_marker:
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
        observed = _observed(document, session_id, now(), licence)
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
    payload: IO[bytes], session_id: str, moment: datetime, licence: HookLicence | None
) -> ObservedAgentEvent | None:
    """Read a bounded payload and keep only the fields a notification is built from."""
    document = _payload_document(payload)
    return None if document is None else _observed(document, session_id, moment, licence)


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
    document: Mapping[str, object],
    session_id: str,
    moment: datetime,
    licence: HookLicence | None,
) -> ObservedAgentEvent | None:
    """Keep only the fields the hook's licence admits for this event.

    The event name is matched exactly against the licence's own list, not through
    `_plain_token`: OpenCode's names carry a dot, and widening the token reader to admit one
    would loosen the guard on `reason` and on every `ask` at the same time to make one
    provider's vocabulary fit. An exact list is narrower than the reader rather than wider.
    Only an event the licence does not list is read as a token, and only for a licence that
    admits unlisted events at all.

    Every field below is read through the licence's tuples and nothing else, so a field no
    vertical declared never reaches disk (DEC-013, DEC-037); what crosses is bounded by
    `bounded_detail_line` or `_plain_token`, because the far end of the spool measures against
    the same budget.
    """
    if licence is None:
        return None
    event = document.get("hook_event_name")
    if not isinstance(event, str):
        return None
    fields = licence.events.get(event)
    if fields is None:
        if licence.unlisted_events is None or _plain_token(event) is None:
            return None
        fields = licence.unlisted_events
    if licence.admits is not None and not licence.admits(document):
        return None
    return ObservedAgentEvent(
        session_id=session_id,
        event=event,
        reason=_first(document, fields.discriminating_fields, _plain_token),
        detail=(
            _ask_detail(document)
            if fields.detail_from_tool_input
            else _first(document, fields.detail_fields, bounded_detail_line)
        ),
        observed_at=moment.astimezone(UTC),
        ask=_first(document, fields.ask_fields, _plain_token),
    )


#: What a `PermissionRequest`'s nested `tool_input` may contribute as DETAIL (DEC-098).
#:
#: Two keys, measured on both providers the same day
#: (`docs/acceptance-2026-09-19-ask-payloads.md`): Codex `Bash` and Claude `Bash` each carry
#: `command` and `description`, and Codex `apply_patch` carries `command` alone. That the two
#: agents agree on these names is why one reader serves both rather than one per provider.
#:
#: **Read by name, one level down, and no further.** `tool_input` is the only nested object this
#: spool descends into, and it descends exactly one level: a recursive walk would turn every future
#: tool's payload into detail sight unseen, which is the assumption Claude's licence
#: (`claude/hooks.py`, its discriminating fields) exists to warn about. A tool carrying neither key
#: yields no detail and still spools its record.
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
