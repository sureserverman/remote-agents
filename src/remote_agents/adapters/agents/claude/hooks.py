"""Claude's hook configuration: which file, which events — the machinery lives in
`adapters.agents.hook_settings` and asks this value (DEC-063)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from remote_agents.adapters.agents.hook_settings import _HookProvider
from remote_agents.ports.agent_activity import ActivityKind, ActivitySource
from remote_agents.ports.provider_descriptor import EventLicence, HookLicence, TurnEdge

INSTALLED_EVENTS = (
    "Stop",
    "StopFailure",
    "Notification",
    "PermissionRequest",
    "UserPromptSubmit",
    "SessionStart",
)
"""`PermissionRequest` joined 2026-09-19 (DEC-098).

It is what makes a Claude ask say what it is asking: the `Notification` this service already
installed carries the constant `Claude needs your permission` and nothing about the ask, so
every Claude `needs_answer` read identically -- 86 of them since 2026-09-01.

**Adding it means an operator MUST re-run `install-agent-hooks`, and the cost of not doing so
is not cosmetic.** `permission_prompt` left `_NOTIFICATIONS` in the same change, because on a
re-installed host it is the wordless twin of this event. A host that upgrades the service but
not its hooks therefore has neither: no `PermissionRequest` group in its settings, and no
mapping for the `Notification` that used to stand in. Its Claude approvals stop producing a
`needs_answer` **at all** -- silently, with nothing logged, until the installer is re-run.

An earlier draft of this paragraph said such a host "will go on reporting through
`Notification` alone… the pre-DEC-098 behaviour rather than a broken one". That was wrong in
the direction that matters: it describes graceful degradation where the real behaviour is the
loss of the whole approval channel, which is the one this product exists for at 3am. Two
independent reviews caught it.

**`UserPromptSubmit` joined 2026-09-24 (BL-108, DEC-104)** and starts the session's turn marker;
the hook reads the event name and never the prompt. Here, unlike `PermissionRequest`, a host
that has not re-run the installer loses nothing: with no marker the relay reads the screen alone,
as it did before. Claude reads its hooks when a session starts, so a running session gains the
marker only once it is relaunched.

**`SessionStart` joined 2026-10-30 (DEC-135)** and leaves the session's started mark, the agent's
own word that it came up, for a launch whose warnings hold the banner off the screen. Measured in
`tests/live/test_claude_session_start_hook.py`. A host that has not re-run the installer loses
nothing it had: readiness reads the screen and the conversation mark, as before.
"""

RETIRED_EVENTS = ("SessionEnd",)
"""Events this installer used to own and must still clean up after.

**Without this, dropping an event strands it for ever.** `_without_our_groups` opens by
skipping any event it does not own, so an event removed from `INSTALLED_EVENTS` stops being
inspected at all -- and our group under it is copied across untouched by *both* the install
path and the uninstall path, which share the predicate. The hook would go on firing on every
host with none of this project's tooling able to remove it, and it could not be worked around
by uninstalling first, because that would mean running the *old* uninstall before taking the
upgrade.

So the sweep is over what we own **now or ever did**, and an event leaves `INSTALLED_EVENTS`
by moving here rather than by disappearing. An entry stays until every host has run the
installer at least once since the event was dropped; there is no way for this process to know
when that is, and the cost of keeping one is a dictionary lookup per install.

`SessionEnd` was dropped 2026-08-23 (DEC-051): its record was spooled, read, deleted and then
discarded at the mapping, because there is no `ActivityKind` for it -- `ended` was retired for
reporting an exit the owner had just caused. It wrote a file per session end, in every Claude
session on the machine, that nothing ever consumed.
"""


#: Flagless: claude's hook commands predate `--provider` and a flagged reinstall would add a
#: second entry beside every existing one instead of replacing it.
PROVIDER = _HookProvider(
    "claude", Path(".claude/settings.json"), INSTALLED_EVENTS, RETIRED_EVENTS, flagless=True
)

#: What every Claude event other than an ask may keep: the field that discriminates it into a
#: kind, and one line of the agent's own words.
#:
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
#: `tests/live/test_agent_activity_hooks.py` is where that comparison now lives. (Moved here
#: from `activity_spool._DISCRIMINATING_FIELDS` with the values, BL-050.)
#: `reason` was `SessionEnd`'s, and `SessionEnd` is retired (DEC-051); since BL-131 an event
#: the licence does not list writes no record at all, so a host still firing it spools nothing.
_RECORD = EventLicence(
    discriminating_fields=("error", "notification_type"),
    detail_fields=("message", "last_assistant_message"),
)


def _paused_on_background_work(document: Mapping[str, object]) -> bool:
    """Whether this payload is a `Stop` that only paused for Claude's own background work.

    Claude ends its turn while a background shell, subagent, monitor or workflow still runs,
    fires `Stop`, and wakes itself with a new turn when that work reports back. Every such pause
    used to read as "finished", so one piece of work could notify several times (the owner's
    ask, 2026-10-17). The payload's `background_tasks` lists what is still in flight, and is
    empty when nothing is: measured on Claude Code 2.1.302, a `Stop` with a `sleep` running in
    the background carried one `shell` entry, and the `Stop` after its wake-up carried `[]`.

    A paused `Stop` is still recorded, marked: the service holds it back, and delivers it as
    "finished" only if the session stays quiet -- a dev server left running never reports back,
    and must not silence the session for good (DEC-129).

    Only that list is read, and only for its length; no entry reaches disk. An absent list is
    an older Claude that cannot say, so its `Stop` is an end, as it always was.
    `session_crons` is not consulted: a session waiting on a schedule has finished what it was
    doing, and a looping one would otherwise never say "finished" until its quiet spell.
    """
    if document.get("hook_event_name") != "Stop":
        return False
    pending = document.get("background_tasks")
    return isinstance(pending, list) and bool(pending)


#: Only the reasons that answer "why did it stop". Every other value -- authentication_failed,
#: and whatever upstream adds next -- is absent on purpose, and a `StopFailure` carrying one
#: writes no record. (Moved here from `application.activity._STOP_FAILURES`, BL-131.)
_STOP_FAILURES = MappingProxyType(
    {
        "rate_limit": ActivityKind.LIMIT_REACHED,
        # Not the same news, and not the same next move: a rate limit is waited out, an output
        # ceiling is continued from. One sentence for both told the owner the alarming one.
        "max_output_tokens": ActivityKind.OUTPUT_LIMIT,
    }
)

#: Which `Notification` types mean something this service will say out loud. (Moved here from
#: `application.activity._NOTIFICATIONS`, BL-131; the history of each type is kept there.)
#:
#: `permission_prompt` left on 2026-09-19 (DEC-098): every one shares a `prompt_id` with a
#: `PermissionRequest` for the same ask, so mapping it sent a wordless twin. `idle_prompt` was
#: never mapped: a sixty-second idle timer answers "why did it stop" wrongly too often.
#: `agent_needs_input` comes from the `claude agents` view, when a background session it lists
#: changes to blocked (measured, BL-048), and is filed under the pane that runs the view -- hence
#: its lead.
_NOTIFICATIONS = MappingProxyType({"agent_needs_input": ActivityKind.NEEDS_ANSWER})

AGENTS_VIEW = "from the agents view"
"""Where an `agent_needs_input` notice came from, at the front of its detail on both surfaces.

At the front because the console cuts a detail at its own width (`feed._elide`), and the label
before it can be the opening words of a long prompt. Stored in the detail, the field both
surfaces already render, so nothing new is stored (owner, 2026-11-18)."""

HOOK_LICENCE = HookLicence(
    events=MappingProxyType(
        {
            "Stop": EventLicence(
                kind=ActivityKind.COMPLETED,
                turn=TurnEdge.ENDS,
                discriminating_fields=_RECORD.discriminating_fields,
                detail_fields=_RECORD.detail_fields,
            ),
            "StopFailure": EventLicence(
                kinds_by_reason=_STOP_FAILURES,
                turn=TurnEdge.ENDS,
                discriminating_fields=_RECORD.discriminating_fields,
                detail_fields=_RECORD.detail_fields,
            ),
            "Notification": EventLicence(
                kinds_by_reason=_NOTIFICATIONS,
                detail_lead_by_reason=MappingProxyType({"agent_needs_input": AGENTS_VIEW}),
                discriminating_fields=_RECORD.discriminating_fields,
                detail_fields=_RECORD.detail_fields,
            ),
            # DEC-098: the tool class as the ask, and the ask's own words from `tool_input`.
            # `_RECORD`'s fields are not consulted -- an ask carries no `message` and no
            # `last_assistant_message`, and reading fields that cannot be present would only
            # obscure which one was expected.
            "PermissionRequest": EventLicence(
                kind=ActivityKind.NEEDS_ANSWER,
                ask_fields=("tool_name",),
                detail_from_tool_input=True,
            ),
            # Starts the marker; the hook reads the event name and never the prompt (DEC-104).
            "UserPromptSubmit": EventLicence(turn=TurnEdge.STARTS),
        }
    ),
    source=ActivitySource.HOOK_EXCLUSIVE,
    reports=frozenset(
        {
            ActivityKind.COMPLETED,
            ActivityKind.LIMIT_REACHED,
            ActivityKind.OUTPUT_LIMIT,
            ActivityKind.NEEDS_ANSWER,
        }
    ),
    paused=_paused_on_background_work,
    started_event="SessionStart",
)
"""What Claude's activity hook may record (BL-050); the descriptor's `activity` is this value."""
