"""Codex's hook configuration: which file, which events — the machinery lives in
`adapters.agents.hook_settings` and asks this value (DEC-067)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from remote_agents.adapters.agents.hook_settings import _HookProvider
from remote_agents.ports.agent_activity import ActivityKind, ActivitySource
from remote_agents.ports.provider_descriptor import EventLicence, HookIdentity, HookLicence

#: `UserPromptSubmit` joined 2026-09-24 (BL-108, DEC-104) to start the turn marker. Codex 0.155.1
#: was measured firing it by that name, beside `SessionStart`, in a hook-event log that day. Two
#: live drills prove it: `tests/live/test_codex_activity_hooks.py` under `codex exec`, and
#: `tests/live/test_prompt_relay.py`, which waits for the marker in an interactive `codex` pane.
#: If a Codex build renamed it, the marker would simply never start and the relay would read the
#: screen alone, as before.
PROVIDER = _HookProvider(
    "codex", Path(".codex/hooks.json"), ("Stop", "PermissionRequest", "UserPromptSubmit")
)

#: The rollout key naming who answers a Codex escalation. Measured 2026-10-01 on codex-cli
#: 0.159.3: `turn_context` carries it at `payload.approvals_reviewer`, and a mid-session
#: `/permissions` change writes `thread_settings_applied` with it at
#: `payload.thread_settings.approvals_reviewer`. Re-measure when Codex changes either shape.
_REVIEWER_KEY = b'"approvals_reviewer"'
#: The reviewers that answer without the owner: the binary's enum is `user`, `auto_review` and
#: `guardian_subagent`, the older name for the same reviewer. An allowlist, so a value Codex
#: adds later is heard rather than silenced.
_AUTOMATIC_REVIEWERS = frozenset({"auto_review", "guardian_subagent"})
_ROLLOUT_CHUNK_BYTES = 1024 * 1024
#: How far back the scan looks before it gives up and lets the ask through. The measured
#: setting sat 4.6 MB from the end; a rollout that never names a reviewer must not cost every
#: ask a read of the whole file inside Codex's hook.
_ROLLOUT_SCAN_BYTES = 64 * 1024 * 1024


def _the_owner_answers(document: Mapping[str, object]) -> bool:
    """Whether this payload is one the owner, not Codex's own reviewer, will answer.

    Only a `PermissionRequest` can be answered by someone else; every other admitted event is
    the owner's to see.
    """
    return document.get("hook_event_name") != "PermissionRequest" or not _reviewer_decides(document)


def _reviewer_decides(document: Mapping[str, object]) -> bool:
    """Whether Codex's own reviewer, not the owner, answers this session's escalations (DEC-112).

    Codex fires `PermissionRequest` for every escalation, including the ones its `auto_review`
    reviewer then approves without showing the owner anything. On 2026-10-01 one such session
    had produced about 340 `needs_answer` notifications in two days, none of them a question
    the owner could see. The payload does not say who reviews -- its `permission_mode` enum has
    no value for it -- so the session's own rollout is read for the newest setting.

    The transcript path is used here and never leaves the hook's process (DEC-013 clause 2).
    Anything unreadable answers `False`, so a missed setting costs a notification, never a
    silent ask. The catch is broad for that reason: the caller swallows every exception and
    spools nothing, which here would be the silent ask.
    """
    try:
        path = document.get("transcript_path")
        if not isinstance(path, str) or not path or not Path(path).is_file():
            return False
        return _newest_reviewer(Path(path)) in _AUTOMATIC_REVIEWERS
    except Exception:
        return False


def _newest_reviewer(path: Path) -> str | None:
    """Scan a rollout backwards for the newest record naming its reviewer.

    Not bounded to a tail: on the measured session the newest setting sat 4.6 MB from the end,
    nine times the tail the usage readers take, because a single long turn writes none.
    """
    try:
        with path.open("rb") as handle:
            end = handle.seek(0, os.SEEK_END)
            floor = max(0, end - _ROLLOUT_SCAN_BYTES)
            position = end
            pending = b""
            while position > floor:
                step = min(_ROLLOUT_CHUNK_BYTES, position - floor)
                position -= step
                handle.seek(position)
                pending = handle.read(step) + pending
                # The first line is whole only at the start of the file; otherwise it waits
                # for the next chunk to complete it.
                start = 0 if position == 0 else pending.find(b"\n") + 1
                if start == 0 and position > 0:
                    continue
                window, pending = pending[start:], pending[:start]
                for line in reversed(window.splitlines()):
                    if _REVIEWER_KEY in line:
                        reviewer = _reviewer_of(line)
                        if reviewer is not None:
                            return reviewer
    except OSError:
        return None
    return None


def _reviewer_of(line: bytes) -> str | None:
    """The reviewer a `turn_context` or `thread_settings_applied` record names, else None."""
    try:
        record = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        return None
    payload = record["payload"]
    if record.get("type") == "turn_context":
        source = payload
    elif record.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
        source = payload.get("thread_settings")
    else:
        return None
    reviewer = source.get("approvals_reviewer") if isinstance(source, dict) else None
    return reviewer if isinstance(reviewer, str) else None


#: What a Codex payload may contribute, per event. **Measured, never assumed** --
#: `docs/acceptance-2026-08-29-codex-activity-detail.md` records the field vocabulary of real
#: payloads captured against a disposable `CODEX_HOME`, and `Stop`'s `detail_fields` below are its
#: licensing section written as code. Deliberately narrower than Claude's detail fields: `message`
#: was never observed on a Codex payload, and a field this project has not seen is not a field it
#: reads.
#:
#: `Stop` is the only event with detail fields. `PermissionRequest`'s detail is composed separately,
#: by the spool's `_ask_detail` (`detail_from_tool_input`), because its words live one level down
#: inside `tool_input` rather than at the top level `detail_fields` are read from.
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
#: `detail`, but into `ask`, a field of its own (`PermissionRequest`'s `ask_fields`, DEC-074). This
#: paragraph is kept because its argument is why the two fields are separate; it is annotated
#: because a reader who stopped at it would otherwise leave with the wrong conclusion.
#:
#: **Amended 2026-09-19 by DEC-098, and this is the paragraph a reader must not stop at.** The
#: argument above concluded that a bare provider token is not prose and so does not belong in
#: `detail`. That conclusion stands and is why `ask` still exists. What DEC-098 reverses is the
#: separate refusal of `tool_input`: the owner is the sole operator and the sole recipient, so
#: their own command is not a leak, and `tool_input.description` is not a token at all -- it is
#: the agent's own one-sentence reason, which is precisely what `detail` has always meant. The
#: words now ride in `detail` beside the class in `ask`; neither field took the other's job.
#:
#: What a Codex payload may contribute as an ASK CLASS, per event. `PermissionRequest` only, and
#: `tool_name` only -- the one field the measurement
#: (`docs/acceptance-2026-08-29-codex-activity-detail.md`) licenses, whose own licensing section
#: reads "`PermissionRequest` -> `tool_name` at most, and nothing else" -- a licence **amended on
#: 2026-09-19 by DEC-098**, which additionally admits `tool_input`'s two measured keys into
#: `detail`; the ask field, which is about `ask` alone, is unchanged by that. `Stop` admits none: an
#: agent that has finished is not waiting on anything.
#:
#: Widened on 2026-08-30 from "every payload field discarded" to "the one measured field a
#: notification renders" -- which supersedes nothing. DEC-013 clause (2) already allows a hook to
#: keep "one bounded single line of detail" beside the event name, session id and time, and
#: DEC-063 kept that clause binding while replacing only DEC-013's obsolete claim that Codex has no
#: usable source. Codex simply was not using an allowance Claude has had since the spool was
#: written; this brings it to parity. DEC-063's content-free claim is scoped to the pane-*title*
#: watcher, which is untouched and still retains one boolean.
#:
#: `tool_name` is read as a plain token, not a detail line. The measurement observed it only as
#: `Bash` in all four samples and says so; its value space is unverified beyond that. The narrow
#: reader is what keeps an unverified space from becoming a rendering surface: a value carrying
#: a space, a slash or a quote is not a tool class this project recognises, and it is dropped
#: rather than drawn under the owner's session name.
HOOK_LICENCE = HookLicence(
    events=MappingProxyType(
        {
            # Measured, never assumed: `docs/acceptance-2026-08-29-codex-activity-detail.md`.
            # `message` was never observed on a Codex payload, so only the finished turn's last
            # words are read -- a field this project has not seen is not a field it reads.
            "Stop": EventLicence(detail_fields=("last_assistant_message",)),
            # `tool_name` as the ask (DEC-074), and the ask's own words from `tool_input`
            # (DEC-098). No `reason`: nothing here discriminates a Codex event into a kind.
            "PermissionRequest": EventLicence(
                ask_fields=("tool_name",), detail_from_tool_input=True
            ),
        }
    ),
    starts_turn_marker=True,
    # Hooks plus a pane-title watch: Codex escalates natively past its hook (DEC-063).
    source=ActivitySource.HYBRID,
    # Stop and PermissionRequest only. Codex has no StopFailure equivalent, so neither limit kind
    # is *reported*: a limit stop is read off its pane by `application.limit_stops`'s
    # `LimitScreenWatcher` and arrives INFERRED -- an inference with a measured marker behind
    # it, not a claim this licence makes about the hook.
    reports=frozenset({ActivityKind.COMPLETED, ActivityKind.NEEDS_ANSWER}),
    admits=_the_owner_answers,
    # Codex runs every turn, and so every hook, in its per-home app-server daemon. The daemon
    # carries the environment of whichever pane started it, so the variable names the wrong
    # session -- measured 2026-10-13 on codex-cli 0.162.1 (DEC-126).
    identity=HookIdentity.PAYLOAD_THREAD,
    # Set per thread in each tool shell measured; the shell's session variable is the daemon
    # starter's, like a hook's -- measured 2026-10-16 on codex-cli 0.162.1 (DEC-128).
    thread_variable="CODEX_THREAD_ID",
)
"""What Codex's activity hook may record (BL-050); the descriptor's `activity` is this value."""
