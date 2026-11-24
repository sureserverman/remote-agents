"""Telegram session list, details and session-specific actions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from math import ceil

from remote_agents.adapters.telegram.inspection import InspectionResult
from remote_agents.adapters.telegram.labels import (
    _ATTACH_EMOJI,
    _INSPECT_EMOJI,
    _MESSAGE_EMOJI,
    _REMOTE_EMOJI,
    _RENAME_EMOJI,
    _ROLLOVER_EMOJI,
    action_button_label,
)
from remote_agents.adapters.telegram.limits_block import limits_block
from remote_agents.adapters.telegram.navigation import _button_rows, _page_number, notice_line
from remote_agents.adapters.telegram.presenters import (
    UNIFORM_ROW_WIDTH,
    Button,
    RenderedMessage,
    bounded_text,
)
from remote_agents.adapters.telegram.screen import ScreenActions
from remote_agents.adapters.telegram.screen import reply_arguments as _reply_arguments
from remote_agents.adapters.telegram.stops import CONFIRMED_FORCE, StopController
from remote_agents.application.commands import (
    AnswerTrustCommand,
    DeclineTrustCommand,
    InspectQuery,
    RemoteControlCommand,
)
from remote_agents.application.errors import SessionNotFoundError, StopNotPermittedError
from remote_agents.application.permission_apply import permission_state_line
from remote_agents.application.permission_presets import (
    LIVE_CHOICES,
    permission_change_sentence,
    permission_label,
    permission_refusal_sentence,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    CANCEL_ROLLOVER,
    FORCE,
    NOTE_ROLLOVER,
    REMOTE_CONTROL_LABELS,
    ROLLOVER,
    ROLLOVER_ACTION_LABELS,
    RemoteControlDirection,
    StopFailure,
    available_actions,
    decline_trust_available,
    explain_state,
    message_available,
    pane_is_attachable,
    remote_control_available,
    remote_control_directions,
    remote_control_reading,
    remote_control_refusal,
    remote_control_target,
    trust_available,
)
from remote_agents.application.session_views import (
    RolloverMark,
    SessionGit,
    StateGroup,
    agent_said,
    context_mark,
    git_line,
    group_counts,
    group_emoji,
    limit_rows,
    owner_dialog,
    rollover_marks,
    session_lines,
    session_row_parts,
    state_emoji,
    usage_lines,
    with_token,
)
from remote_agents.application.stops import execute_stop
from remote_agents.domain.models import (
    OrphanProvenance,
    ProfileId,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.trust import TrustState
from remote_agents.domain.trust import answerable as trust_answerable
from remote_agents.ports.agent_usage import AgentLimits, AgentUsage, ContextWindow, LimitsAbsence
from remote_agents.ports.host_remote_control import CodexRemoteControlBusy
from remote_agents.ports.message_relay import (
    CLAIM_ABANDONED_AFTER_FOR_MESSAGES,
    MessageRelay,
    RelayOutcome,
    RelayResult,
    WaitingMessage,
)
from remote_agents.ports.terminal import PromptReason, RemoteControlRefused

_LOG = logging.getLogger(__name__)

_GROUP_TITLES: dict[StateGroup, str] = {
    StateGroup.ACTIVE: "ACTIVE",
    StateGroup.IN_TRANSITION: "IN TRANSITION",
    StateGroup.NEEDS_ATTENTION: "NEEDS ATTENTION",
    StateGroup.PRESERVED: "PRESERVED",
}
"""What each bucket is headed on the sessions list. The bucket is the shared decision
(`session_views.StateGroup`); the heading is this surface's sentence (DEC-043)."""

_BACK_TO_SESSIONS = "\u2039 Back to sessions"  # ‹ Back to sessions

_FACT_LABEL_WIDTH = 8
"""`context`, `remote`, `pane` padded to eight columns, so the values in the detail's fact block
start in one column."""

_RELAY_REASON_WORDS: dict[PromptReason, str] = {
    PromptReason.EMPTY: "the message was empty",
    PromptReason.SHELL: "a message starting with ! would run as a shell command",
    PromptReason.NOT_RUNNING: "the session isn't running",
    PromptReason.NO_COMPOSER: "this agent's input can't be read",
    PromptReason.BUSY: "the agent is working",
    PromptReason.BUSY_ON_SCREEN: "the agent is working",
    PromptReason.COMPOSING: "the agent's input already holds text",
    PromptReason.DIALOG: "the agent is asking a question",
    PromptReason.UNRECOGNISED: "the agent's screen wasn't recognised",
    PromptReason.DRAFT_NOT_SEEN: "the text never showed in the input, so Enter wasn't pressed",
    PromptReason.MENU: "the agent's command menu couldn't confirm that / command",
    PromptReason.SUBMIT_NOT_SEEN: "Enter was pressed, but the input wasn't seen to clear",
    PromptReason.TIMEOUT: "tmux didn't answer in time",
    PromptReason.KEYS_BUSY: "something else was typing into the session",
    PromptReason.TMUX_ERROR: "tmux failed partway through",
}
"""Why a relayed message did not simply go, in the owner's words (DEC-099). Every reason has
one, so a new reason fails the Send message flow tests rather than reaching the chat as a key."""

_REMOTE_CONTROL_ACTIONS: dict[RemoteControlState, str] = {
    RemoteControlState.ACTIVE: "Turn it on",
    RemoteControlState.INACTIVE: "Turn it off",
}

_REMOTE_CONTROL_QUESTIONS: dict[RemoteControlState, str] = {
    RemoteControlState.ACTIVE: "Remote Control is on. Turn it off?",
    RemoteControlState.INACTIVE: "Remote Control is off. Turn it on?",
    RemoteControlState.UNKNOWN: "Remote Control could not be read. Turn it on?",
}

_REMOTE_CONTROL_WORDS: dict[RemoteControlState, str] = {
    RemoteControlState.ACTIVE: "on",
    RemoteControlState.INACTIVE: "off",
}
"""How the detail's `remote` fact line reads the last observation. Anything else is `unknown`.

Since the button became a toggle this line is the only place the bot still *states* a
direction, which is the right place for these words: it reports what the pane was last seen
doing, where the button can only propose. The two are read at different moments and may
honestly disagree — this line is as old as the last toggle, the confirmation below is as old
as the press."""

_ROLLOVER_CALLBACKS: dict[str, str] = {
    ROLLOVER: "session.rollover",
    NOTE_ROLLOVER: "session.rollover.note",
    CANCEL_ROLLOVER: "session.rollover.cancel",
}
"""The press each shared rollover action rides on (DEC-046). Under `session.` so the marker
stays on the sessions tab, as every other press about one session does."""

_ROLLOVER_OF_CALLBACK = {callback: action for action, callback in _ROLLOVER_CALLBACKS.items()}

_ROLLOVER_MUTATIONS = frozenset({ROLLOVER, NOTE_ROLLOVER})
"""The rollover presses claimed once: each writes a request that later types, launches and
stops sessions unattended. A cancel is not, for `session.unqueue`'s reason."""

_QUEUED_LINE_WIDTH = 48
"""How much of a waiting message's first line the session screen shows."""


def _state_explanation(state: SessionState, orphan_provenance: OrphanProvenance | None) -> str:
    """Defer to the shared mapping so both surfaces describe a state identically."""
    return explain_state(state, orphan_provenance)


def git_facts(read: SessionGit | None) -> tuple[tuple[str, str], ...]:
    """The detail's `git` fact, or none: the shared `git_line`, labelled at `_FACT_LABEL_WIDTH`
    like every other fact (DEC-091). No summary -- no reader, a folder outside git, a read that
    failed or ran out of time -- draws no line rather than one saying so."""
    if read is None:
        return ()
    return (("git", git_line(read.summary, shared=read.shared)),)


def session_state_line(
    record: SessionRecord,
    context: ContextWindow | None,
    rollover: RolloverMark | None,
    token: str,
    *,
    mark_at: int | None = None,
) -> str:
    """A row's monospace second line, `state · age[ · gauge][ · note]`, with the git token after
    it when the whole fits `UNIFORM_ROW_WIDTH`. The identity has a line of its own above, so
    the token can never push it out; at the narrow width the token goes first, whole."""
    _first, second = session_lines(record, context, rollover, mark_at=mark_at)
    return with_token(second, token, UNIFORM_ROW_WIDTH)


def _first_line(text: str) -> str:
    """A waiting message as its first non-blank line, cut to fit the session screen."""
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(line) > _QUEUED_LINE_WIDTH:
        return line[: _QUEUED_LINE_WIDTH - 1] + "…"
    return line


@dataclass
class SessionListState:
    page: int = 1


@dataclass(kw_only=True)
class SessionScreens(ScreenActions):
    list_state: SessionListState
    mark_screen: Callable[[str], None]
    _host_remote_block: Callable[[], Awaitable[str]]
    _claude_default_block: Callable[[], Awaitable[str]]
    _inspection_result: Callable[[str], Awaitable[InspectionResult | None]]
    _listed_records: Callable[[], Awaitable[tuple[SessionRecord, ...]]]
    _record: Callable[[str], Awaitable[SessionRecord | None]]
    _session_name: Callable[[str], Awaitable[str]]
    glyphs: Mapping[str, str]
    profiles: tuple[ProfileAvailability, ...]
    trust_dialogs: Mapping[str, object]
    message_relay: MessageRelay | None
    relayable: frozenset[str]
    session_page_size: int
    stops: StopController

    async def _sessions_reply(
        self,
        page: int = 1,
        *,
        notice: str | None = None,
        mark: bool = True,
        records: tuple[SessionRecord, ...] | None = None,
    ) -> RenderedMessage:
        """Render one page of managed sessions: grouped, two lines a row, a picker per row.

        This list is unbounded in a way the project list is not — every launch adds a row
        and only reconciliation takes one away — so it pages for the same reason: a keyboard
        tall enough to push the message off the screen is unusable on a phone, and Telegram
        caps the buttons one keyboard may carry.

        **The rows live in the message text, not on the buttons.** A keyboard button cannot
        hold a newline, and the two-line row is the redesign's whole answer to a list nobody
        could scan: the identity in bold on one line, the state, age and gauge in monospace
        on the next. The buttons become short pickers -- `🟢 <mark> #7 remote-agents`, the
        mark being the agent's (`glyphs`), absent for a profile that declares none -- in the same
        order as the text, two to a row.

        **Grouped by `StateGroup`, in its fixed order, and a bucket with nothing in it is not
        drawn.** The grouping is the shared decision (`session_views.state_group`); the
        headings are this surface's words. The header's emoji legend is the count: `Sessions
        · 6  🟢 2 · 🟡 1 · 🔴 2 · ⚪ 1`, counted from the *whole* listing rather than the page,
        and never from a second read -- two reads can disagree, because a session can end
        between them. A bucket at zero is left out of the legend for the same reason it is
        left out of the body.

        `notice` is the lead line an action that *ended somewhere else* leaves here — the
        outcome of a stop, which lands on this list rather than on a screen of its own.
        It is escaped because it carries wording derived from a `StopFailure`, and it is
        rendered above the heading rather than below it so the owner reads what happened
        before they read the list it happened to. It reaches the empty branch too.

        **Budget.** A page of eight two-line rows over the limits block of four agents at their
        widest measures about 1,050 UTF-16 units against `MAX_TELEGRAM_TEXT_UNITS`, so the page
        size stays at eight.
        Every string here passes `escape()` and then `presenters._message` (DEC-014).
        """
        # Handed in by the store-driven redraw, which has already read them to decide whether
        # this page is worth drawing at all. Every other caller reads them here as before.
        records = await self._listed_records() if records is None else records
        counts = group_counts(records)
        legend = " · ".join(
            f"{group_emoji(group)} {count}" for group, count in counts.items() if count
        )
        heading_counts = f" · {len(records)}" + (f"  {legend}" if legend else "")
        # Read once, above the branch, because both branches render it. The empty branch needs
        # it for the reason `notice` already reaches there: stopping the last session is exactly
        # when this list is empty, and an agent's week window does not stop existing because
        # nothing is running against it right now.
        spent = await self._limit_block()
        # Read beside the limits and for the same reasons: both branches render it, and the
        # empty one needs it most -- a machine's enrollment does not stop being a fact because
        # nothing is running against it right now.
        host = await self._host_remote_block()
        # Read beside the host's, not under it: the two subjects are independent settings, so
        # a composition that wired one and not the other must still get the one it wired.
        claude = await self._claude_default_block()
        # Marked here, above the empty-list return, and not once at the bottom. Below the
        # return it missed the "Nothing is running." screen entirely -- so the one page an
        # owner is most likely to have open when they launch was the one page a store change
        # could never redraw, which is the 0 -> 1 transition this whole mechanism is for.
        #
        # `mark=False` is for the caller that already knows what it built and renders it
        # itself. The mark is shared state read after an await, so a caller that does not need
        # one must not leave one where a concurrent render can take it.
        if mark:
            self.mark_screen("sessions")
        if not records:
            self.list_state.page = 1
            # No body Launch. It was the way out before a permanent way out existed; the
            # bar now carries the identical destination on the very next row, and a button
            # duplicating the one directly beneath it reads as a bug.
            return self._message(
                f"{notice_line(notice)}<b>Sessions</b>{heading_counts}\n"
                f"Nothing is running.{spent}{host}{claude}",
                self._schedules_entry(),
            )
        page_count = max(1, ceil(len(records) / self.session_page_size))
        index = min(max(page, 1), page_count)
        # After the clamp, not before: `_sessions_page` is read as the page to *return* to, so
        # it has to be a page that exists. A request past the end renders the last one, and
        # remembering the request rather than the render would send Back somewhere emptier.
        self.list_state.page = index
        start = (index - 1) * self.session_page_size
        shown = records[start : start + self.session_page_size]
        sections: list[str] = []
        pickers: list[Button] = []
        # Through the one backend, as the local surface reads it (DEC-046, DEC-091), and as
        # total as it is there: a rollover read that fails costs the notes, never the list.
        try:
            rollovers = await rollover_marks(
                self.backend.rollovers,
                shown,
                sessions=self.backend.sessions,
                catalogue=self.backend.catalogue,
            )
        except Exception:
            _LOG.debug("the session rollover marks could not be read", exc_info=True)
            rollovers = {}
        gits = await self._git_reads(shown)
        said = await self._agent_said(shown)
        for group in StateGroup:
            members = [record for record in shown if session_row_parts(record).group is group]
            if not members:
                continue
            lines = [f"{group_emoji(group)} <b>{_GROUP_TITLES[group]}</b>"]
            for record in members:
                context = await self._context_for(record)
                rolled = rollovers.get(str(record.session_id))
                read = gits.get(str(record.session_id))
                mark_at = self.backend.mark_at.get(str(record.profile_id))
                parts = session_row_parts(
                    record, context, rolled, read and read.summary, mark_at=mark_at
                )
                second = session_state_line(record, context, rolled, parts.git, mark_at=mark_at)
                # The sequence sits *outside* the bold, so the eye lands on the name and
                # finds the number beside it, and the state line is monospace so the gauges
                # of neighbouring rows line up.
                words = said.get(str(record.session_id))
                lines.append(
                    f"<b>{escape(parts.identity)}</b> #{parts.sequence}\n"
                    f"<code>{escape(second)}</code>"
                    # The agent's claim under the row's measured line, never inside it, and cut
                    # to the row's width: eight rows of the drain's 240-character bound, escaped,
                    # would pass a message's 4096 units and cost the whole list. The detail
                    # carries the line whole.
                    + (
                        ""
                        if words is None
                        else f"\n{escape(bounded_text(words, limit=UNIFORM_ROW_WIDTH))}"
                    )
                )
                # The agent's mark between the state and the sequence: the state group is
                # what the eye scans first, and the mark answers "which of these two is
                # which" once it has landed on the group. A profile with no mark -- any
                # composition wired without the registry, and a fifth provider until it
                # declares one -- collapses to exactly the label drawn before this existed,
                # separator and all, rather than to one carrying a hole where a mark goes.
                glyph = self.glyphs.get(str(record.profile_id), "")
                mark = f"{glyph} " if glyph else ""
                pickers.append(
                    Button(
                        f"{state_emoji(record.state)} {mark}#{parts.sequence} "
                        f"{record.display.project_slug}",
                        self._callback("session.detail", str(record.session_id)),
                    )
                )
            sections.append("\n".join(lines))
        buttons = list(_button_rows(tuple(pickers), 2))
        navigation = []
        if index > 1:
            navigation.append(Button("Previous", self._callback("sessions.page", str(index - 1))))
        if index < page_count:
            navigation.append(Button("Next", self._callback("sessions.page", str(index + 1))))
        if navigation:
            buttons.append(tuple(navigation))
        title = "Sessions" if page_count == 1 else f"Sessions {index}/{page_count}"
        body = "\n\n".join(sections)
        return self._message(
            f"{notice_line(notice)}<b>{title}</b>{heading_counts}\n\n{body}{spent}{host}{claude}",
            tuple(buttons) + self._schedules_entry(),
        )

    def _schedules_entry(self) -> tuple[tuple[Button, ...], ...]:
        """The sessions page's way to the schedules, where this host manages any."""
        if self.backend.schedules is None:
            return ()
        return ((Button("⏰ Schedules", self._callback("schedules.open", "schedules")),),)

    async def _context_for(self, record: SessionRecord) -> ContextWindow | None:
        """One RUNNING session's context window for its row gauge, or nothing at all.

        Read only for a RUNNING row -- `session_row_parts` draws no gauge for any other state,
        so a read there would be a provider sweep for a figure nobody renders. The broad
        `except` is `_usage`'s trade: the gauge is a decoration on a list whose real
        content is the way into each session, and a provider that changed its file format must
        not cost the owner the list. Reads are not cached here; a page holds at most eight rows
        and the bot drew this screen on a press and never on a timer.

        **The second half of that sentence stopped being true on 2026-09-12** and is kept
        because it is the premise the no-caching decision rested on. This page is now redrawn
        when the *store* changes, so these reads are paid per write rather than per press.
        What bounds them is not a timer either: `_REDRAW_INTERVAL_SECONDS` floors edits at one
        per two seconds, which caps this at four provider reads a second on a page of eight
        during a burst of launches, and at nothing at all when the store is quiet. Judged
        worth it rather than overlooked -- but a cache belongs here the day that floor is
        lowered or the page grows.
        """
        if self.backend.usage is None or record.state is not SessionState.RUNNING:
            return None
        try:
            usage = await self.backend.usage(record.session_id)
        except Exception:
            logging.getLogger(__name__).debug("usage read failed", exc_info=True)
            return None
        return None if usage is None else usage.context

    def _sessions_back(self) -> str:
        """A Back that lands on the page of the sessions list the owner actually left.

        `sessions.page` rather than `sessions.open`, because the two differ by exactly the
        thing this fixes: `sessions.open` renders `_sessions_reply()` at its default first
        page. Opening a row from page 3 and pressing Back used to answer page 1, and the only
        way back to page 3 was Refresh — which is now gone, so this is the whole route.

        `sessions.open` keeps meaning *the top of the list*, which is what the navigation
        bar's Sessions button, `/sessions` and `/start` should all do. Only the detail, which
        was opened from a known page, is entitled to return to one.
        """
        return self._callback("sessions.page", str(self.list_state.page))

    async def _detail_reply(self, session_value: str, message_id: int = 0) -> RenderedMessage:
        record = await self._record(session_value)
        if record is None:
            # Reached by opening a row that ended under the owner, so the list they came
            # from is exactly where they need to go — not Home, and not its first page.
            return self._message(
                "That session is no longer available.",
                back=self._sessions_back(),
                back_label=_BACK_TO_SESSIONS,
            )
        # **These read-only rows are the surfaces' one deliberate divergence, and the four
        # axes are written down here so nobody has to re-derive them from a diff again.**
        # Against `adapters/tui/screens/sessions.py: detail_entries`:
        #
        #   1. order — here Inspect, Rename, [Copy attach], Remote Control; there [Copy
        #      attach], [Inspect], Rename, Remote Control;
        #   2. Inspect is unconditional here and gated on `backend.capture is not None`
        #      there;
        #   3. Copy attach is gated on `_attach_row_is_offered` here and unconditional
        #      there, which is the row's *presence* and is a separate question from DEC-021's
        #      ownership predicate — that rule is now `pane_is_attachable`, shared, and the
        #      gate applies it rather than restating it (Task 3.4);
        #   4. the label — "Inspect" here, "Inspect output" there.
        #
        # Everything *below* these rows is already shared: the trust row reads
        # `trust_available`, the remote-control rows `remote_control_directions`, the stops
        # `available_actions` + `ACTION_LABELS`, and the parity contract pins the last two on
        # both surfaces. So the detail action set is not a duplicate awaiting a merge — it is
        # three shared groups and one divergent head, and unifying any row of the table above
        # is a functionality change. Owner's decision, 2026-08-22, recorded in the
        # shared-use-cases sub-plan under Task 2.3; a later stage that wants one assembler has
        # to parameterize exactly these four axes.
        #
        # **Shape, since the redesign: read-only actions pair up, stops keep their own row.**
        # Inspect and Rename share the first row; Copy attach and the Remote Control direction
        # share the second, each present only where its gate says so. The marks in front of
        # the labels are this surface's (`_ACTION_EMOJI` and friends); the words stay shared.
        reads: list[Button] = [
            Button(f"{_INSPECT_EMOJI} Inspect", self._callback("session.inspect", session_value)),
            # Renaming changes what the session is called and nothing about what it is doing,
            # so it sits with the reads.
            Button(f"{_RENAME_EMOJI} Rename", self._callback("session.rename", session_value)),
        ]
        buttons: list[tuple[Button, ...]] = [tuple(reads)]
        attachable = await self._attach_row_is_offered(record)
        second: list[Button] = []
        if attachable:
            second.append(
                Button(
                    f"{_ATTACH_EMOJI} Copy attach",
                    self._callback("session.attach", session_value),
                )
            )
        # One button per direction the policy still offers -- which is one once this
        # session's state has been observed, and both only while it is unknown. Half of the
        # old pair was always a no-op, on the deepest screen the bot has.
        for direction in remote_control_directions(record, record.remote_control_state):
            second.append(
                Button(
                    f"{_REMOTE_EMOJI} {REMOTE_CONTROL_LABELS[direction]}",
                    self._callback("remote.control", f"{session_value}|{direction.value}"),
                )
            )
        if second:
            buttons.append(tuple(second))
        # The relay's row (DEC-099): Send message where the relay can type into this agent,
        # and Cancel beside it while a message waits. Above the trust and stop rows, and
        # never in them: typing into a session is not answering its question or ending it.
        waiting = self._waiting_message(record.session_id)
        relay_row: list[Button] = []
        if self.message_relay is not None and message_available(
            record.state, record.profile_id, self.relayable
        ):
            relay_row.append(
                Button(
                    f"{_MESSAGE_EMOJI} Send message",
                    self._callback("session.message", session_value),
                )
            )
        if waiting is not None:
            # Not a mutation token, unlike trust and decline: a second press of a cancel finds
            # nothing waiting and says so, which is harmless where a replayed keypress is not.
            relay_row.append(
                Button("Cancel queued message", self._callback("session.unqueue", session_value))
            )
        if relay_row:
            buttons.append(tuple(relay_row))
        # Rollover now / Rollover with a note / Cancel rollover (DEC-046, DEC-115, BL-119):
        # exactly what the shared policy
        # offers over the switch and this session's open rollover, read through
        # `Backend.rollovers`. A row of its own, never the stop row's: asking for a rollover
        # writes one row and stops nothing. Both asks are mutation tokens, claimed once,
        # because the request it writes is what later launches and stops sessions unattended;
        # a cancel is not, for `session.unqueue`'s reason -- a second press finds nothing.
        rollover_row = tuple(
            Button(
                f"{_ROLLOVER_EMOJI} {ROLLOVER_ACTION_LABELS[action]}",
                self._callback(
                    _ROLLOVER_CALLBACKS[action],
                    session_value,
                    mutation=action in _ROLLOVER_MUTATIONS,
                ),
            )
            for action in await self._rollover_offer(record)
        )
        if rollover_row:
            buttons.append(rollover_row)
        # Permissions (DEC-033): one action, then one pick, as on the console. Offered where a
        # change could be asked for at all; the use case refuses the rest by name.
        if self._live_permissions_offered(record):
            buttons.append(
                (
                    Button(
                        "🛡 Permissions",
                        self._callback("session.permissions", session_value),
                    ),
                )
            )
        # **The two answers share one row, and the row is still the trust row's own.** The
        # owner asked for the pair to sit side by side on 2026-09-11, which supersedes
        # DEC-032's clause that gave each answer a row of its own; what that clause was
        # protecting survives as the separation below. The stop row comes next, it is two-wide
        # when the state offers two stops, and on a surface with no separators the row is the
        # only grouping there is -- so answering the question and ending the session must
        # never land in the same row. `test_the_trust_row_never_touches_the_stop_row` pins it.
        answers: list[Button] = []
        # A dialog only the owner may answer is said as a fact below and gets no trust row,
        # whatever the trust reading says: it is the dialog drawn, and nothing is pressed on it
        # (BL-146). One pane read, for both.
        waits = await owner_dialog(self.backend.sessions, record)
        if waits is None and await self._awaiting_trust(record):
            answers.append(
                Button(
                    "Trust this project",
                    # A mutation token: this button sends a keypress into a live pane, so
                    # it is claimed once and never replayed, exactly like the confirmed
                    # stop and resume buttons.
                    self._callback("session.trust", session_value, mutation=True),
                )
            )
        # Neither answer over a dialog only the owner may answer: a decline reads no folder
        # question there and presses nothing, so its button could never act (Stage 3 gate).
        if waits is None and decline_trust_available(record):
            # **Offered on a different condition from the button beside it, so the row is
            # two-wide only when both hold.** Saying yes means typing into the agent's
            # dialog, so it is confined to the profiles this project can read and costs a
            # pane capture to decide. Saying no does not: it ends a session that never
            # started, which is reachable for codex and cursor-agent too and needs only the
            # record. So a session whose dialog nobody parses gets exactly one of these two
            # buttons -- alone in the row, one wide -- rather than a Trust that would refuse
            # itself when pressed.
            #
            # This is the fallback rather than the primary route -- the notification is --
            # and it exists because DEC-049 abandons a message after three refusals, which
            # would otherwise leave that session with no way to be answered at all.
            answers.append(
                Button(
                    "Don't trust — close it",
                    self._callback("session.decline", session_value, mutation=True),
                )
            )
        if answers:
            buttons.append(tuple(answers))
        # The stops share one row of their own. Telegram has no separator, so shape and the
        # mark are the only signals available, and the actions that end a session should not
        # look like the ones that read it — a graceful stop is one tap from discarding the
        # pane's output. No state offers more than two stops, so the row stays legible; force
        # is last because `available_actions` puts it last.
        stops: list[Button] = []
        for action in available_actions(record.state, record.orphan_provenance):
            token = self.stops.offer(
                record.session_id,
                record.profile_id,
                record.state,
                record.orphan_provenance,
                action,
                self.owner_user_id,
                self.owner_chat_id,
            )
            if token is not None:
                stops.append(Button(action_button_label(action), token))
        if stops:
            buttons.append(tuple(stops))
        # The status line replaces `State: running`: the same mark the list uses, the shared
        # state word, the short age. Then the explanation, unchanged. Then the fact block --
        # `<code>` lines with the label padded to eight so the values align, and a line is
        # drawn only where it has something to say. Escaped like every other line here even
        # where the value is digits and fixed words — the escape is a property of this
        # boundary (DEC-014), not a judgement about each string's provenance.
        parts = session_row_parts(record)
        facts: list[tuple[str, str]] = []
        if waits is not None:
            facts.append(("dialog", waits))
        settings = self.backend.permission_presets
        if settings is not None and record.profile_id in settings.profiles:
            try:
                preset = await self.backend.sessions.permission_preset(record.session_id)
                label = (
                    permission_label(preset) if preset is not None else "unknown (legacy session)"
                )
                # Whether the daemon confirmed it, in the sentence both surfaces share.
                applied = permission_state_line(
                    preset, await self.backend.sessions.permission_state(record.session_id)
                )
            except Exception:
                _LOG.exception("the launch permission snapshot could not be read")
                label, applied = "unknown (snapshot unavailable)", None
            fact = f"Launch permissions: {label} (requested at launch)"
            facts.append(("permissions", fact if applied is None else f"{fact}; {applied}"))
        read, usage = await self._usage(record)
        # No reader, or a read that failed, draws no usage line at all; a read that answered
        # nothing is `usage_lines`' own sentence.
        for value in usage_lines(usage) if read else ():
            facts.append(("context", value))
        # The owner's threshold, said under the context it is about (BL-119), in the row's words.
        marked = (
            context_mark(usage.context, self.backend.mark_at.get(str(record.profile_id)))
            if usage is not None and record.state is SessionState.RUNNING
            else None
        )
        if marked is not None:
            facts.append(("context", marked))
        facts.extend(git_facts(await self._git_read(record)))
        if remote_control_available(record):
            facts.append(
                (
                    "remote",
                    "Remote Control "
                    + _REMOTE_CONTROL_WORDS.get(record.remote_control_state, "unknown"),
                )
            )
        if attachable:
            facts.append(("pane", "attachable"))
        if waiting is not None:
            facts.append(("queued", _first_line(waiting.text)))
        fact_lines = "".join(
            f"\n<code>{label.ljust(_FACT_LABEL_WIDTH)} {escape(value)}</code>"
            for label, value in facts
        )
        # The agent's own words after the measured facts, outside their `<code>` block and
        # under their own label: a claim, not a fact (BL-122). Escaped like every line here.
        said = (await self._agent_said((record,))).get(str(record.session_id))
        said_line = "" if said is None else f"\n{escape(said)}"
        return self._message(
            f"<b>{escape(parts.identity)}</b> #{parts.sequence}\n"
            f"<code>{state_emoji(record.state)} {escape(parts.state)} · {parts.age}</code>\n"
            f"{_state_explanation(record.state, record.orphan_provenance)}"
            f"{fact_lines}{said_line}",
            tuple(buttons),
            back=self._sessions_back(),
            back_label=_BACK_TO_SESSIONS,
        )

    async def _relay_reply(self, session_value: str, result: RelayResult) -> RenderedMessage:
        """Say what became of the owner's message, and offer the way back to its session.

        A queued reply carries its own Cancel, because the owner has just been told a message
        will be typed in later and the moment to change their mind is now, not after finding
        the session again.
        """
        name = await self._session_name(session_value)
        why = _RELAY_REASON_WORDS.get(result.reason, "") if result.reason else ""
        buttons: tuple[tuple[Button, ...], ...] = ()
        if result.outcome is RelayOutcome.SENT:
            text = f"Sent to {name}."
        elif result.outcome is RelayOutcome.QUEUED:
            text = f"Queued — {why}. It will be typed in when {name} next finishes."
            if result.replaced:
                text += " It replaced the message that was waiting."
            buttons = (
                (
                    Button(
                        "Cancel queued message",
                        self._callback("session.unqueue", session_value),
                    ),
                ),
            )
        elif result.outcome is RelayOutcome.UNCONFIRMED:
            # Not "Sent": `Enter` may never have been pressed, and the text may be sitting in
            # the agent's input, where the next `Enter` at the desk would submit it.
            text = (
                f"Typed into {name}, but not confirmed — {why}. The text may still be in the "
                "agent's input; check the session before sending it again."
            )
        else:
            text = f"Not sent — {why}."
        return self._message(
            text,
            buttons,
            back=self._callback("session.detail", session_value),
            back_label="Back to session",
        )

    def _waiting_message(self, session_id: SessionId) -> WaitingMessage | None:
        """The session's waiting message, or None -- also when the UI store cannot answer.

        A failed read costs the `queued` line and its Cancel, never the session screen: the
        screen carries the stop buttons, and a locked `ui.sqlite3` must not take them away.
        """
        if self.message_relay is None:
            return None
        try:
            return self.message_relay.pending(session_id)
        except Exception:
            _LOG.exception("reading the waiting message for %s failed", session_id)
            return None

    async def _rollover_offer(self, record: SessionRecord) -> tuple[str, ...]:
        """The rollover actions this session offers, or none where the backend wires no book.

        Total: a failed read costs the row, never the detail it sits on.
        """
        book = self.backend.rollovers
        if book is None:
            return ()
        try:
            return await book.offered(record, self.backend.auto_rollover)
        except Exception:
            _LOG.exception("reading the rollover offer for %s failed", record.session_id)
            return ()

    async def _rollover_reply(
        self, action: str, session_value: str, token: str, message_id: int
    ) -> dict[str, object]:
        """Rollover now or Cancel rollover, pressed: one row written or withdrawn, then the
        shared outcome words (`RolloverBook.press`). Nothing is launched, typed or stopped here;
        the pass in `serve` acts on the request at the workflow's own gate (brief §32)."""
        book = self.backend.rollovers
        record = await self._record(session_value)
        if book is None or record is None:
            return _reply_arguments(
                self._message(
                    "That session is no longer available.",
                    back=self._sessions_back(),
                    back_label=_BACK_TO_SESSIONS,
                )
            )
        if action in _ROLLOVER_MUTATIONS and not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            said = await book.press(action, record, self.backend.auto_rollover)
        except Exception:
            _LOG.exception("the rollover press %s for %s failed", action, session_value)
            said = "The rollover could not be recorded; nothing was changed."
        return _reply_arguments(
            self._message(
                escape(said),
                back=self._callback("session.detail", session_value),
                back_label="Back to session",
            )
        )

    def _live_permissions_offered(self, record: SessionRecord) -> bool:
        settings = self.backend.permission_presets
        return (
            record.state is SessionState.RUNNING
            and settings is not None
            and record.profile_id in settings.profiles
        )

    async def _live_permissions_question(self, session_value: str) -> RenderedMessage:
        """The three live choices (`LIVE_CHOICES`, OD-3), the session's current one marked."""
        back = self._callback("session.detail", session_value)
        record = await self._record(session_value)
        if record is None or not self._live_permissions_offered(record):
            return self._message(
                "That session's permissions cannot be changed now.",
                back=back,
                back_label="Back to session",
            )
        try:
            refusal = await self.backend.sessions.permission_change_refusal(record.session_id)
        except Exception:
            # Offered as before: a pick is still refused by the use case if it must be.
            _LOG.exception("whether %s's permissions can change could not be read", session_value)
            refusal = None
        if refusal is not None:
            # Said before any mode is offered, not after a pick (close-out demo, DEC-091).
            return self._message(
                escape(permission_refusal_sentence(refusal)),
                back=back,
                back_label="Back to session",
            )
        try:
            state = await self.backend.sessions.permission_state(record.session_id)
        except Exception:
            _LOG.exception("the permission state of %s could not be read", session_value)
            state = None
        current = None if state is None else state.requested
        choices = tuple(
            (
                Button(
                    choice.label + (" ✓" if choice.value == current else ""),
                    self._callback(
                        "session.permissions.set",
                        f"{choice.value.value}:{session_value}",
                        mutation=True,
                    ),
                ),
            )
            for choice in LIVE_CHOICES
        )
        return self._message(
            "<b>Permissions</b>\nChange this session's mode now. Its continuations keep it; "
            "the Settings default stays unchanged.",
            choices,
            back=back,
            back_label="Back to session",
        )

    async def _live_permissions_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """A pick: claimed once, so a redelivered press changes nothing (DEC-011), then the
        shared outcome sentence (DEC-091)."""
        value, _, session_value = entity_id.partition(":")
        back = self._callback("session.detail", session_value)
        try:
            preset = PermissionPreset(value)
        except ValueError:
            preset = None
        if preset not in {choice.value for choice in LIVE_CHOICES}:
            return _reply_arguments(self._message("That permission selection is invalid."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            said = permission_change_sentence(
                await self.backend.change_session_permissions(
                    SessionId.parse(session_value), preset
                )
            )
        except Exception:
            _LOG.exception("the permission change for %s failed", session_value)
            said = "not changed: the change could not be recorded"
        return _reply_arguments(
            self._message(escape(said), back=back, back_label="Back to session")
        )

    def _unqueue_reply(self, session_value: str) -> RenderedMessage:
        session_id = SessionId.parse(session_value)
        # Read before cancelling: a claimed message is being typed right now, and removing its
        # row cannot take back keys already sent. Saying "cancelled" there would be the one
        # answer the owner could not trust.
        waiting = self._waiting_message(session_id)
        cancelled = self.message_relay is not None and self.message_relay.cancel(session_id)
        if not cancelled:
            text = "Nothing was waiting — the message had already been sent or dropped."
        elif (
            waiting is not None
            and waiting.claimed_at is not None
            # An abandoned claim is nobody typing: the store re-claims it after this long, so
            # "it was already being typed in" would describe a process that died (BL-107).
            and datetime.now(UTC) - waiting.claimed_at < CLAIM_ABANDONED_AFTER_FOR_MESSAGES
        ):
            text = (
                "Cancelled, but it was already being typed in, so it may still arrive. "
                "You'll be told if it does."
            )
        else:
            text = "Queued message cancelled."
        return self._message(
            text,
            back=self._callback("session.detail", session_value),
            back_label="Back to session",
        )

    async def _limit_block(self) -> str:
        """Each agent's rate-limit windows, as a monospace block under the rows.

        Last on the screen and separated by a blank line, because it is a statement about the
        account rather than about any row — the placement that stops a window reading as the
        spend of whichever session it happens to sit beside, which is the report this block
        exists to answer. `limits_block` lays it out, and it carries what the terminal's limits
        pane carries: a row per agent that publishes limits or is a switch away from it, its
        bars, reset countdowns, pace, and its source and age (DEC-061).

        **The row set is the profile set's**, as it is on the terminal: an agent that answered
        nothing keeps its row and says which silence it is.

        **A failed read shows every agent as unreadable, never a vanished block.** A block that
        disappeared read as "no limits to report", which is a different answer. The broad
        `except` is `_usage`'s trade made again one screen up, and it matters more here:
        this block sits on the screen that is the only way to reach a session at all, so a
        provider that changed its file format under an upgrade must not be able to cost the
        owner the list.
        """
        if self.backend.limits is None:
            return ""
        profiles = tuple(ProfileId(profile.profile_id) for profile in self.profiles)
        try:
            rows = limit_rows(await self.backend.limits(), profiles)
        except Exception:
            logging.getLogger(__name__).debug("account limits read failed", exc_info=True)
            rows = limit_rows(
                tuple(
                    AgentLimits(profile, absence=LimitsAbsence.UNREADABLE) for profile in profiles
                ),
                profiles,
            )
        block = limits_block(rows)
        return f"\n\n{block}" if block else ""

    async def _usage(self, record: SessionRecord) -> tuple[bool, AgentUsage | None]:
        """Ask the provider what this session has spent, and never let the answer cost a screen.

        `(read, usage)`: `read` is False when there is no reader or the read failed. A host that
        wired no reader renders no usage line at all — the same absence-is-an-answer
        arrangement `capture` and `activity_feed` have. The broad `except` is the trade
        `ProfileUsageReaders` already makes one layer down, made again at the seam that
        matters: the usage lines are a decoration on a screen whose real content is a session's
        state and its stop actions.
        """
        if self.backend.usage is None:
            return False, None
        try:
            return True, await self.backend.usage(record.session_id)
        except Exception:
            logging.getLogger(__name__).debug("usage read failed", exc_info=True)
            return False, None

    async def _agent_said(self, records: Sequence[SessionRecord]) -> dict[str, str]:
        """The `agent said:` line of each record that has one, from one read for them all.

        `_usage`'s trade made again: the line is a decoration, and a feed that cannot be
        read costs the line, never the screen."""
        try:
            return await agent_said(
                self.backend.last_words, [str(record.session_id) for record in records]
            )
        except Exception:
            logging.getLogger(__name__).debug("the agent's last words failed", exc_info=True)
            return {}

    async def _git_read(self, record: SessionRecord) -> SessionGit | None:
        """One session's git summary, or nothing -- never a screen lost to a git read.

        `_usage`'s trade made again: the line is a decoration on a screen whose real
        content is the session's state and its stop actions. The read is bounded at the root
        (two seconds) and answers None itself on any failure; this `except` is the seam's own
        guarantee, so a reader that broke its promise still costs only the line."""
        if self.backend.git_summary is None:
            return None
        try:
            return await self.backend.git_summary(record.session_id)
        except Exception:
            logging.getLogger(__name__).debug("git summary read failed", exc_info=True)
            return None

    async def _git_reads(self, records: Sequence[SessionRecord]) -> dict[str, SessionGit]:
        """Every shown row's git summary, read side by side: a page of eight sequential reads
        could cost eight budgets on a slow disk, side by side it costs one."""
        reads = await asyncio.gather(*(self._git_read(record) for record in records))
        return {
            str(record.session_id): read
            for record, read in zip(records, reads, strict=True)
            if read is not None
        }

    async def _attach_reply(self, session_value: str) -> RenderedMessage:
        record = await self._record(session_value)
        back = self._callback("session.detail", session_value)
        # One question, asked once. This used to check `_can_copy_attach` and *then* call
        # `copy_attach`, so it inspected the same pane twice to reach one answer — and the
        # first of those two asked a rule the adapter had restated for itself. `copy_attach`
        # applies `pane_is_attachable` and returns nothing when it does not hold, which is the
        # same refusal by the same rule, so the extra round trip bought exactly nothing.
        command = await self._attach_command(record)
        if command is None:
            return self._message(
                "Copy Attach is unavailable: this session has no pane on this host any more.",
                back=back,
            )
        return self._message(
            f"<b>Copy attach command</b>\n<code>{escape(command)}</code>", back=back
        )

    async def _attach_command(self, record: SessionRecord | None) -> str | None:
        """The copyable command for this pane, or `None` when the owner may not be given one.

        `sessions is None` is a host with no session use case, which cannot answer at all; it
        is the arm the removed predicate opened with, kept because the answer is still "no
        command" rather than an attribute error mid-render.

        `SessionNotFoundError` is caught for the same reason and is **not** belt-and-braces:
        the removed `_can_copy_attach` reached only `inspect`, which answers `None` for a
        session that has gone, so a row drawn just before the record ended used to earn the
        refusal sentence below. `copy_attach` opens with `_require_session` instead, and
        `session.attach` has no `_PENDING_NOTICES` entry, so an uncaught raise here reaches
        `callback`'s `if pending is None: raise` and costs the owner the screen rather than a
        sentence. The window is narrow — between `_record`'s list read and this call — but it
        is the one behavioural difference this task would otherwise have made, and the task
        forbids one.
        """
        if record is None or self.backend.sessions is None:
            return None
        try:
            return await self.backend.sessions.copy_attach(record.session_id)
        except SessionNotFoundError:
            return None

    async def _attach_row_is_offered(self, record: SessionRecord) -> bool:
        """Whether the detail draws a Copy attach row at all — this surface's own choice.

        **Named for the question it answers rather than for the rule it applies**, because the
        two are genuinely different and Task 2.3 wrote the difference down: the local surface
        renders this row unconditionally and explains when it is chosen, so *row presence* is
        the bot's alone (axis 3 of the four the detail's read-only head diverges on). What is
        no longer the bot's is the rule inside it — `pane_is_attachable` is the one DEC-021
        requires identical on both surfaces, and it is now asked rather than restated.

        Still one `inspect` and no store read, which is what the removed `_can_copy_attach`
        cost. Asking `copy_attach` here instead would have been the shorter diff and the wrong
        one: it re-reads the record and builds a command string a row-presence test then
        throws away, turning every detail render into two pane inspections. It would also move
        a question from action time to render time, which `tests/support/backends.py` records
        as a real distinction rather than an incidental one.
        """
        if self.backend.sessions is None:
            return False
        observation = await self.backend.sessions.inspect(InspectQuery(record.session_id))
        return pane_is_attachable(observation, record)

    async def _remote_control_confirm_reply(self, entity_id: str) -> RenderedMessage:
        """Read the pane, then ask the owner about the one direction that reading implies.

        This screen is where the toggle stops being a toggle. The button that reached it
        carries no direction at all — it cannot, because the state it would have to name is
        only knowable by looking — so the reading is taken here, once, and the mutation
        callback below carries the explicit ACTIVE or INACTIVE that `RemoteControlCommand`
        has always wanted. DEC-003's confirmation is untouched: there is still exactly one
        press between the owner and a live pane, and it still names what it will do.

        Availability is re-asked through `remote_control_available` rather than by re-testing
        the profile alone. The old guard let a session that had left RUNNING since the screen
        was drawn through to a confirmation the policy would no longer offer; asking the same
        function the button asked keeps the two from disagreeing (DEC-007).
        """
        session_value, separator, direction_value = entity_id.partition("|")
        if not separator or direction_value != RemoteControlDirection.TOGGLE.value:
            return self._message("That Remote Control request is incomplete.")
        if self.backend.sessions is None:
            return self._message("Remote Control is unavailable.")
        record = await self._record(session_value)
        if record is None or not remote_control_available(record):
            return self._message("Remote Control is unavailable for this session.")
        reading = remote_control_reading(
            await self.backend.sessions.remote_control_state(record.session_id),
            record.remote_control_state,
        )
        desired = remote_control_target(reading)
        question = _REMOTE_CONTROL_QUESTIONS[reading]
        action = _REMOTE_CONTROL_ACTIONS[desired]
        return self._message(
            f"<b>{question}</b>\nThis uses only the verified Claude interaction.",
            (
                (
                    Button(
                        action,
                        self._callback(
                            "remote.confirm",
                            f"{session_value}|{desired.value}",
                            mutation=True,
                        ),
                    ),
                ),
                (Button("Cancel", self._callback("session.detail", session_value)),),
            ),
        )

    async def _remote_control_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        if self.backend.sessions is None:
            return _reply_arguments(self._message("Remote Control is unavailable."))
        session_value, separator, state_value = entity_id.partition("|")
        # The membership test moved down from the confirm screen, which no longer receives a
        # direction to check: its own entity now reads `<session>|toggle`, and this one is
        # the only place a direction still arrives from a button. Without it a malformed
        # entity reaches `RemoteControlState(state_value)` and raises rather than answering.
        if not separator or state_value not in {
            RemoteControlState.ACTIVE.value,
            RemoteControlState.INACTIVE.value,
        }:
            return _reply_arguments(self._message("That Remote Control request is incomplete."))
        # Re-read before the claim, for the reason `_launch_reply` gives: the session this
        # button names may have ended since the screen was drawn, and spending the one-shot
        # on it would answer the retry with "already run".
        if await self._record(session_value) is None:
            return _reply_arguments(self._message("That session is no longer available."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        state = RemoteControlState(state_value)
        try:
            result = await self.backend.sessions.set_remote_control(
                RemoteControlCommand(SessionId.parse(session_value), state, token)
            )
        except RemoteControlRefused as refused:
            return _reply_arguments(self._message(remote_control_refusal(refused)))
        # A refusal is answered above with its reason; what remains is a reading.
        # Just the resulting state. Distinguishing "was already active" from "is now active"
        # is not knowable here: `remote_control` returns the desired state both when it had
        # to send keys and when it found the pane already there, so a message claiming the
        # difference would be asserting something this layer cannot see. The port would have
        # to say whether it acted, which is a wider change than this screen's wording.
        return _reply_arguments(self._message(f"Remote Control: {result.value}."))

    async def _awaiting_trust(self, record: SessionRecord) -> bool:
        """Whether to offer the trust row: `untrusted`, and the pane still asking.

        **Two gates, and neither is sufficient alone.** The record says whether this is a
        session the lifecycle believes is blocked on the question; the pane says whether the
        dialog is still on screen this instant. A row offered on the record alone would
        outlive an answer given at the keyboard; a row offered on the pane alone is the defect
        below.

        **The state gate was removed once, deliberately, and is back for a reason that did
        not exist then.** It gated on FAILED and STARTING, on the reasoning that a
        trust-blocked launch never becomes ready — and that hid the button on the first real
        session to hit the bug, because `claude-remote` prints its readiness banner *before*
        the dialog renders, so the launch loop can report RUNNING over an agent stuck on a
        question. The conclusion drawn was that state says nothing and the pane is the only
        authority.

        What has changed is how the race is handled. The launch settle is sized from
        measurement so that the launch itself catches a late dialog (codex: 0.75 s against a
        worst gap of 0.46 s over 60 launches, `docs/acceptance-2026-10-09-codex-trust-settle.md`)
        and records `untrusted`, and the button follows the record. A dialog that outruns the
        settle anyway gets no row; it is answered at the keyboard (DEC-047), which is the cost
        DEC-081 accepted.

        **What the pane-only version cost, which is why that trade is worth taking.** The
        classifier decides "this pane is asking" by matching text, and text is not proof that
        a dialog is drawn — an agent displaying a file that *contains* the dialog matches it
        exactly, and this repository's own trust fixtures are such a file. So a healthy
        RUNNING session showed a live *Trust this project* button, and pressing it sent
        movement keys and an Enter into a working agent. No marker count closes that (a
        verbatim copy satisfies any number of them); asking the lifecycle does. DEC-080.

        **What the sibling actually shares, corrected.** This used to end by saying the
        notifier "has always filtered this way", so that the state gate was "the detail screen
        agreeing with its sibling rather than a new rule". That is true of **one** of the two
        gates and was read as both. `trust_notifications.py` shares the *state* filter — its
        pass skips any record that is not `UNTRUSTED` — and does **not** share the pane read:
        it offers Trust on `answerable(profile_id, ...)` alone. Its one pane read, for a dialog
        only the owner may answer (BL-146), can only withhold the answers, never offer one.

        **That asymmetry is correct and deliberate, not a gap to close.** This screen is
        re-rendered on demand, so a capture taken as it draws describes the pane the owner is
        looking at. A notification is sent once and then outlives its own read by any amount of
        time, so a pane gate there would pin a button to an observation that was already stale
        when it was minted — ceremony, not safety. What actually protects that press is one
        layer down and unconditional: `TmuxRuntime.answer_trust` re-reads the pane before
        sending anything, and since BL-053's second finding it reports honestly when it refused
        rather than returning the same value as success (`ports.terminal.TrustAnswer`). So the
        notification's button cannot fire a keypress into a session that is no longer asking,
        and a press that read nothing now says so instead of claiming the folder was trusted.

        **What DEC-081 changed, implemented 2026-10-09.** A pane may **clear** `untrusted` and
        may no longer **set** it: the reconciler and the list-open re-read return no event for
        a pane showing the dialog, and the state machine has no TRUST_REQUIRED edge from
        anything but STARTING. So the state half of this gate can no longer be conjured by an
        agent *displaying* the dialog's words mid-session -- only a launch or resume settle
        puts a record where this gate looks. One residual is left: a *resume* redraws its old
        conversation during its settle, and one about this repo can carry the words, so that
        resume is recorded UNTRUSTED (DEC-081).
        """
        if record.state is not SessionState.UNTRUSTED:
            return False
        if self.backend.sessions is None or not trust_available(
            record, TrustState.AWAITING, self.trust_dialogs
        ):
            # Asked with AWAITING as a hypothetical: if the answer is False even then, the
            # record alone rules the row out (wrong profile) and the pane never has to be
            # read. Only a session that *could* be answered costs a capture.
            return False
        state = await self.backend.sessions.trust_state(record.session_id)
        return trust_available(record, state, self.trust_dialogs)

    async def _trust_reply(self, entity_id: str, token: str, message_id: int) -> dict[str, object]:
        if self.backend.sessions is None:
            return _reply_arguments(self._message("Answering the trust question is unavailable."))
        # Re-read before the claim, for the reason `_launch_reply` and `_remote_control_reply`
        # both give: this button outlives the screen that drew it. The profile is re-checked
        # here too, and claiming first meant a refused press still burned the one-shot, so the
        # retry answered "already run" for a button that had never worked once.
        #
        # **This check is now the only one of its kind on this route, and that is deliberate.**
        # `SessionService.answer_trust` used to raise on a profile it could not answer, against
        # a hand-written list; it does not any more, because the authority moved to the
        # terminal's injected declarations — an agent with no dialog declared gets `UNKNOWN`
        # from `answer_trust` and no key is pressed. So this is a surface-side refusal with a
        # sentence for the owner, not the outer half of two agreeing guards: removing it would
        # not reopen a keypress, but it would replace "that agent does not ask this question"
        # with a silent no-op.
        record = await self._record(entity_id)
        if record is None:
            return _reply_arguments(self._message("That session is no longer available."))
        if not trust_answerable(record.profile_id, self.trust_dialogs):
            return _reply_arguments(
                self._message("That session's agent does not ask this question.")
            )
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            result = await self.backend.sessions.answer_trust(
                AnswerTrustCommand(SessionId.parse(entity_id), token)
            )
        except CodexRemoteControlBusy:
            # Refused before any key went in (BL-037): the question is still on screen. A
            # fresh button, because this one's token is spent.
            return _reply_arguments(
                self._message(
                    "Codex Remote Control is changing on this machine, so nothing was sent. "
                    "Press Trust again in a moment.",
                    (
                        (
                            Button(
                                "Trust",
                                self._callback("session.trust", entity_id, mutation=True),
                            ),
                        ),
                    ),
                )
            )
        except ValueError:
            # **The refusal DEC-080 added, given a sentence.** The service refuses a press for
            # a record that is no longer `untrusted`, which is exactly the stale button this
            # screen cannot help minting: the owner answers at the keyboard, the record moves
            # on, and an older copy of the question is still in the chat. Uncaught, that
            # reached the dispatcher's re-raise — `session.trust` had no pending notice then;
            # it has one now, added by this same change — and
            # the owner got a cleared spinner and no words at all, which reads as the button
            # being broken rather than as the question being over. Its sibling `_decline_reply`
            # has caught the equivalent from the start; this is the trust half catching up.
            return _reply_arguments(
                self._message(
                    "That question has already been answered, so the session is no longer "
                    "waiting. Nothing was sent to it."
                )
            )
        # **`pressed` first, because it is the only branch that is not about the pane.**
        # Every refusal to type -- no dialog declared, the question already over, or a layout
        # `plan_trust_keys` would not count rows on -- used to arrive here as the same
        # `TrustState.UNKNOWN` the *success* path returns, since answering clears the dialog
        # and the capture taken afterwards stops matching. So this method reported **Trusted.
        # The agent can continue** for a pane nothing had been sent to, after the one-shot
        # token was already spent -- and the retry that sentence invites answers "That action
        # has already run." Failing closed is right; saying it worked is not (BL-053).
        if not result.pressed:
            # **Split on what the pane actually said, because the three refusals are not one
            # story.** A single sentence here claimed the session was "still waiting" for all
            # of them, which is true of exactly one: `plan_trust_keys` failing closed on a
            # layout it will not count rows on. The other two -- the pane not live or no dialog
            # declared, and the question already resolved between this button being drawn and
            # pressed -- are cases where "still waiting" is the same kind of false statement
            # about pane state that BL-053 is about. `observed` is carried precisely so they can
            # be told apart; throwing it away here would reintroduce the defect one notch
            # milder. Found by the Tier-1 review of this task.
            if result.observed is TrustState.AWAITING:
                # On screen, and this project will not type into a layout it cannot read. No
                # "try again": the token is claimed above and will refuse a second press, so
                # inviting one is the same dishonesty one layer along. The keyboard is the
                # route that still works, and DEC-047 is why it is the one named.
                return _reply_arguments(
                    self._message(
                        "No answer was confirmed: the dialog could not be read, or was not yet "
                        "taking keys, so no Enter went to it. The session is still waiting — "
                        "answer it at the keyboard, or close it from its screen."
                    )
                )
            return _reply_arguments(
                self._message(
                    "Nothing was sent — its pane is no longer showing the question. It may "
                    "already have been answered, or the session may no longer be live. Open "
                    "the session to see where it stands."
                )
            )
        # Past here a key was sent, so the pane state is an outcome. UNKNOWN is the expected
        # one and not a failure: answering clears the dialog.
        if result.observed is TrustState.AWAITING:
            # The keys went in and the question is still up. **Not "Try again"** -- that was
            # the same spent-token invitation the refusal branch above refuses to make, left
            # standing here because it predates it. The detail screen re-renders and mints a
            # fresh token, so it is the route that actually works.
            return _reply_arguments(
                self._message(
                    "The keys were sent, but the question is still on screen. Open the session "
                    "and answer it from there."
                )
            )
        return _reply_arguments(
            self._message("Trusted. The agent can continue; relaunch if it already gave up.")
        )

    async def _decline_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """Answer the folder-trust question with *no*, which ends the session (DEC-078).

        Not the only button here that ends a session unconfirmed — Stop and close and Clean up
        both do, per DEC-018. What is particular to this one is that it is the only ending
        offered from a state whose stop policy otherwise carries force alone, which is why it
        needed a decision. The ordering below is the same one `_trust_reply` uses for the same
        reason: everything
        re-derivable is re-derived *before* the one-shot is claimed, so a press against a
        session that has since gone does not burn the token and leave the owner a button that
        answers "already run" for something that never ran.

        The refusal the service can raise is not an error to report as a failure -- it is the
        stale-record case, where the dialog was answered elsewhere while this message sat on
        the owner's phone. They are told what happened rather than that something broke.
        """
        if self.backend.sessions is None:
            return _reply_arguments(self._message("Answering the trust question is unavailable."))
        record = await self._record(entity_id)
        if record is None:
            return _reply_arguments(self._message("That session is no longer available."))
        if not decline_trust_available(record):
            return _reply_arguments(self._message("That session is not waiting to be trusted."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            await self.backend.sessions.decline_trust(
                DeclineTrustCommand(SessionId.parse(entity_id), token)
            )
        except StopNotPermittedError:
            return _reply_arguments(
                self._message(
                    "That question has already been answered, so the session is running. "
                    "Stop it from its own screen if you meant to end it."
                )
            )
        return _reply_arguments(await self._sessions_reply(notice="Closed without trusting."))

    async def _inspect_reply(self, session_value: str) -> RenderedMessage:
        """Render captured output over a way back to the session that produced it.

        Inspecting used to replace the detail view with the capture and a lone Home button,
        so reading a pane cost the owner every action they had just been offered for it.
        That is the wrong trade in general and the wrong one in particular: a graceful stop
        discards the pane's output, so reading before stopping is the only order that works,
        and it was the most expensive path in the bot.
        """
        back = self._callback("session.detail", session_value)
        result = await self._inspection_result(session_value)
        if result is None:
            return self._message("Inspection is unavailable.", back=back)
        return self._message(f"<pre>{escape(result.text)}</pre>", back=back)

    async def _stop_reply(self, action: str, token: str, message_id: int) -> dict[str, object]:
        if action == FORCE:
            return _reply_arguments(await self._force_confirm_reply(token, message_id))
        request = self.stops.claim(token, self.owner_user_id, self.owner_chat_id, message_id)
        if request is None or self.backend.sessions is None:
            # DEC-008 drops the repeat rather than servicing it; where the *answer* is drawn
            # is a separate question, and it lands on the list like every other stop outcome
            # rather than on the one Home-only screen a stop button could still reach.
            return _reply_arguments(
                await self._sessions_reply(notice="That action has already run.")
            )
        # `profile_id` is passed rather than omitted, and that is DEC-006 rather than a
        # detail: `execute_stop` takes it optionally, because the local surface acts on the
        # record under its cursor and has nothing separate to compare. This surface *does* —
        # the token carries the profile the action was offered against — so omitting it here
        # would skip the fail-closed check silently instead of failing. Pinned by
        # `test_a_press_whose_record_changed_profile_never_reaches_the_service`, which drives
        # this press rather than the shared function, because nothing that calls the shared
        # function directly can see whether this call site supplies the argument.
        outcome = await execute_stop(
            request.action,
            request.session_id,
            sessions=self.backend.sessions,
            read_record=lambda: self._record(str(request.session_id)),
            profile_id=request.profile_id,
        )
        if not outcome.dispatched:
            # Lands on the list like every other outcome. Covers all three halves of the
            # guard: the session moved on between the offer and the press, its record is gone
            # entirely, or the profile behind the press is no longer the one in the store.
            # The bot collapses them into one notice deliberately — the local surface words
            # them apart, which is why `execute_stop` reports *which* refusal it was rather
            # than a bare false. The second sentence this used to carry — "Open the list again
            # to see where it is now." — was an instruction to navigate somewhere the owner
            # now already is, so the refusal keeps only the half that says what happened.
            return _reply_arguments(
                await self._sessions_reply(
                    notice="That session moved on before this could run, so nothing was done."
                )
            )
        # `request.action` rather than the pressed one: a confirmed force arrives under an
        # adapter-internal action name, and the outcome is reported in the domain's terms.
        # The record is the one `execute_stop` re-read, not a second read of the store.
        return _reply_arguments(
            await self._stop_outcome_landing(request.action, outcome.record, outcome.failure)
        )

    async def _force_confirm_reply(self, token: str, message_id: int) -> RenderedMessage:
        """Name the session and the cost before offering the only irreversible button.

        The rule is that the destructive button must not be where the thumb already rests,
        and the **order that satisfies it changed** when the navigation bar arrived. Cancel
        used to come first, above `Force stop`, because the row below was a lone `Home`
        nobody pressed — so last-but-one was the safe distance. The bottom row is now the
        bar, the one row in the bot the owner builds muscle memory for, which made
        last-but-one the *worst* position on the screen rather than a safe one.

        So `Force stop` is offered first and Cancel sits between it and the bar: the button
        adjacent to the habitual tap target is the harmless one, and the irreversible one is
        two rows away from it. That also puts this screen in line with every other
        confirmation here — resume, Remote Control, create-project all offer the action
        first and the way out beneath it; force stop was the lone inversion, for a reason
        that has now expired.

        The confirming button is a **new** token carrying a different action, not the one the
        owner just pressed. Re-offering the pressed token cannot work when the screen is
        redrawn in place: the render that draws this screen prunes what the previous keyboard
        left on the message, and the re-offered token is part of exactly that set.
        """
        # A re-read, not a re-resolve: `callback` resolved this token for this message before
        # it dispatched here, and `_release_attachment` and `_abandon_entry` have awaited since.
        # A notification delivered in that gap rebinds the token, and re-asking the message
        # question would refuse the confirmation screen for a force stop that is still legal.
        # The same defect as `StopController.claim`, one window narrower.
        state = self.callbacks.reread(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
        )
        if state is None:
            return self._message("That action is no longer available.")
        session_value, _, profile_value = state.entity_id.partition(":")
        record = await self._record(session_value)
        if record is None:
            return self._message(
                "That session is no longer available.",
                back=self._callback("sessions.open", "sessions"),
            )
        confirmed = self.stops.offer_confirmed_force(
            record.session_id,
            ProfileId(profile_value),
            record.state,
            record.orphan_provenance,
            self.owner_user_id,
            self.owner_chat_id,
        )
        if confirmed is None:
            return self._message(
                "Force stop is no longer available for this session.",
                back=self._callback("session.detail", session_value),
            )
        # The state line is carried onto the confirmation, not only onto the detail screen.
        # Its twin `ForceStopConfirmModal` on the local surface has always rendered it, and
        # after DEC-020 it is what tells the owner they are about to kill a pane this app
        # adopted rather than launched — which is exactly the sentence the one screen that
        # authorises a kill should not be the one screen to omit. The parity contract compares
        # rendered *action sets*, so this divergence was invisible to it.
        return self._message(
            f"<b>Force stop {escape(record.display.rendered)}?</b>\n"
            "This kills the agent immediately and cannot be undone. Anything it has not "
            "saved is lost.\n"
            f"{escape(_state_explanation(record.state, record.orphan_provenance))}",
            (
                (Button(ACTION_LABELS[FORCE], confirmed),),
                (Button("Cancel", self._callback("session.detail", session_value)),),
            ),
        )

    async def _stop_outcome_landing(
        self, action: str, record: SessionRecord, failure: StopFailure | None = None
    ) -> RenderedMessage:
        """Report what the session actually did, named, as the lead line of the session list.

        The outcome used to be a screen of its own whose only exits were `Back` and `Home`,
        which left the owner one dead end away from the list they almost always wanted next.
        It is now the notice on that list: the same words, over the rows they describe. The
        wording is plain text rather than markup because `_sessions_reply` escapes the notice
        once — see `_notice_line` — and that is also what keeps `failure`'s words intact.

        A graceful stop that times out leaves the session RUNNING and removes nothing, so
        "completed" would have been false for the one outcome the owner most needs to act
        on. The record is re-read instead of assumed: `_records()` omits ended sessions, so
        a session that has left the list is one that ended.

        **`failure` is why that re-read is not enough on its own, and it is BL-008.** Finding
        the session still listed says a stop did not take effect; it cannot say which of two
        unrelated things went wrong, because they leave identical evidence. This branch used
        to assert "It did not exit in time" for both — true for `graceful_timeout`, and flatly
        false for `unknown_session`, where no exit sequence was ever sent and the fault is a
        profile this host cannot resolve (DEC-006). Being told the wrong cause is worse than
        being told none: the owner waits for an agent that was never asked to stop.

        The words come from `application.session_actions`, which is where the local surface
        takes them from too — DEC-007 wants the two surfaces to agree about what a stop did,
        and being handed the same sentence is the cheapest form of agreeing. They are escaped
        despite being ours — now by the notice rather than here — because `stop_failure`'s
        fallback interpolates the raw `detail` the terminal adapter reported, which this
        module does not author.
        """
        subject = record.display.rendered
        session_value = str(record.session_id)
        current = await self._record(session_value)
        if current is not None:
            # The `else` is **unreachable today and worded as if it were not**, which is the
            # honest shape for it. `failure` is non-None for graceful and, since force gained
            # the same vocabulary, for a
            # force that found no pane — and `cleanup` and `force_stop` both walk the record to
            # ENDED on every non-raising path (`application/services.py`), so a session still
            # listed after one of those is a state no current code produces. The wording is
            # deliberately neutral
            # about *why* rather than repeating the graceful-stop advice it used to carry:
            # reached at all, this branch is a session that outlived a command that claimed to
            # end it, and telling that operator to wait for a graceful exit would be a guess.
            said = (
                f"{failure.summary} {failure.remedy}"
                if failure is not None
                else (
                    "Nothing was removed and it was left as it is.\n"
                    "Open it again to see where it is now."
                )
            )
            # The session is still listed, so the row the owner needs is on the screen they
            # are about to land on. That is what replaced the "Open session" button this
            # branch used to carry: a list they can act from beats a screen about one session
            # they then have to leave.
            return await self._sessions_reply(notice=f"{subject} is still running\n{said}")
        if failure is not None:
            # The session has left the list and the stop still had something to report. Two
            # producers reach here and the wording has to be true of both, which is why it says
            # what is *observable* — the session is gone from the list — rather than what was
            # done to it.
            #
            # Originally the narrow one: a graceful stop that did not take effect, over a record
            # the other writer DEC-005 permits had ended in the window between the two. The
            # point of threading `failure` here was to stop inferring the outcome from the
            # record, and "Stopped X" over an observation that says nothing was stopped is the
            # reading DEC-006 forbids. Found by the Stage 2 gate's evaluator and its second pass
            # independently.
            #
            # Since force started reporting its own observation, the common one: a force stop
            # that found no managed pane. It killed
            # nothing, the service recorded `VERIFIED_FORCE_STOP` anyway and the record reached
            # ENDED (DEC-017, deliberately — a row the owner cannot clear is the worse failure),
            # so the session really has gone from the list. This branch is reused rather than
            # given its own sentence because DEC-007's shared vocabulary is the safeguard that
            # makes a second surface safe, and a cause worded once per surface is how it stops
            # being shared. The `endings` table below keeps "Force stopped X" for the case where
            # a pane was actually found and killed, which is the only case that may claim it.
            return await self._sessions_reply(
                notice=f"{subject} is no longer listed\n{failure.summary} {failure.remedy}"
            )
        endings = {
            "graceful": (
                f"Stopped {subject}\n"
                "The session has ended. Its pane is gone, so its output is no longer there "
                "to inspect."
            ),
            "cleanup": f"Cleaned up {subject}\nThe session has ended and its pane is gone.",
            "force": f"Force stopped {subject}\nThe session has ended.",
        }
        return await self._sessions_reply(notice=endings[action])

    async def reply_for(
        self, action: str, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        if action == "remote.confirm":
            return await self._remote_control_reply(entity_id, token, message_id)

        if action in {"graceful", "cleanup", "force", CONFIRMED_FORCE}:
            return await self._stop_reply(action, token, message_id)

        if action == "sessions.open":
            return _reply_arguments(await self._sessions_reply())

        if action == "sessions.page":
            return _reply_arguments(await self._sessions_reply(_page_number(entity_id)))

        if action == "session.detail":
            return _reply_arguments(await self._detail_reply(entity_id, message_id))

        if action == "session.attach":
            return _reply_arguments(await self._attach_reply(entity_id))

        if action == "remote.control":
            return _reply_arguments(await self._remote_control_confirm_reply(entity_id))

        if action == "session.trust":
            return await self._trust_reply(entity_id, token, message_id)

        if action == "session.decline":
            return await self._decline_reply(entity_id, token, message_id)

        if action == "session.inspect":
            return _reply_arguments(await self._inspect_reply(entity_id))

        if action == "session.unqueue":
            return _reply_arguments(self._unqueue_reply(entity_id))

        if action == "session.permissions":
            return _reply_arguments(await self._live_permissions_question(entity_id))

        if action == "session.permissions.set":
            return await self._live_permissions_reply(entity_id, token, message_id)

        if action in _ROLLOVER_OF_CALLBACK:
            return await self._rollover_reply(
                _ROLLOVER_OF_CALLBACK[action], entity_id, token, message_id
            )

        return _reply_arguments(self._message("That action is no longer available."))
