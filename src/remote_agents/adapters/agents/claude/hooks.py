"""Claude's hook configuration: which file, which events — the machinery lives in
`adapters.agents.hook_settings` and asks this value (DEC-063)."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from remote_agents.adapters.agents.hook_settings import _HookProvider
from remote_agents.ports.agent_activity import ActivityKind, ActivitySource
from remote_agents.ports.provider_descriptor import EventLicence, HookLicence

INSTALLED_EVENTS = ("Stop", "StopFailure", "Notification", "PermissionRequest", "UserPromptSubmit")
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
#: `reason` is `SessionEnd`'s, and `SessionEnd` is retired (DEC-051). It stays because a host
#: that has not re-run `install-agent-hooks` yet is still firing that hook at this spool, and a
#: reader that stopped recognising the field would mis-parse those records rather than ignore
#: them. The mapping drops the event either way; what this keeps is the ability to read it
#: correctly on the way to being dropped.
_RECORD = EventLicence(
    discriminating_fields=("error", "notification_type", "reason"),
    detail_fields=("message", "last_assistant_message"),
)

HOOK_LICENCE = HookLicence(
    events=MappingProxyType(
        {
            "Stop": _RECORD,
            "StopFailure": _RECORD,
            "Notification": _RECORD,
            # DEC-098: the tool class as the ask, and the ask's own words from `tool_input`.
            # `_RECORD`'s fields are not consulted -- an ask carries no `message` and no
            # `last_assistant_message`, and reading fields that cannot be present would only
            # obscure which one was expected.
            "PermissionRequest": EventLicence(
                ask_fields=("tool_name",), detail_from_tool_input=True
            ),
        }
    ),
    starts_turn_marker=True,
    source=ActivitySource.HOOK_EXCLUSIVE,
    reports=frozenset(
        {
            ActivityKind.COMPLETED,
            ActivityKind.LIMIT_REACHED,
            ActivityKind.OUTPUT_LIMIT,
            ActivityKind.NEEDS_ANSWER,
        }
    ),
    # Any other plain event keeps its record, so an event added upstream is not silently lost
    # and the drain decides what to interpret; the retired `SessionEnd` (DEC-051) is read here.
    unlisted_events=_RECORD,
)
"""What Claude's activity hook may record (BL-050); the descriptor's `activity` is this value."""
