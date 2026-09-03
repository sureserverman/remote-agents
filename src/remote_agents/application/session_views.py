"""What a session looks like in a list, and which directories may be offered as areas.

Both of these were written twice, once per frontend, with byte-identical bodies — the row at
`adapters/telegram/service.py: _session_row_label` and `adapters/tui/model.py: session_row`,
each carrying a comment naming the other (BL-031); the area predicate at
`_selectable_area`/`selectable_area`, docstring included.

**This is the move DEC-029 already made once, finished.** That decision put `state_word` into
`application/session_actions.py` on the argument that what a state is *called* is lifecycle
policy rather than surface rendering, and that byte-identical copies are how the two surfaces
broke together. `state_word` moved; the line that renders it did not, so the two surfaces went
on holding their own copies of the format around a shared word.

**Rendering, not presentation.** These return plain strings for a frontend to place. Nothing
here escapes HTML, measures UTF-16 units, or knows what a keyboard is — DEC-014 keeps the
encodable-once boundary per surface, and a shared renderer sits *upstream* of both presenters
rather than absorbing either. `adapters/telegram/presenters.py` still escapes what it sends
and the Textual side still hands its rows to a widget.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from math import ceil
from typing import Protocol

from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.relative_time import age, age_short, until
from remote_agents.application.session_actions import state_word
from remote_agents.domain.models import OrphanProvenance, SessionRecord, SessionState
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.ports.agent_usage import AgentLimits, AgentUsage, ContextWindow, UsageWindow


def session_row(record: SessionRecord) -> str:
    """One list row. The state word comes from the shared policy, never from `state.value`.

    Both surfaces had this exact line, independently, which is how they rendered both kinds
    of ORPHANED identically (BL-031). `state_word` is the single authority, and now so is
    this.

    The identity is `display.rendered` rather than the generated parts, so a renamed session
    reads under its new name. The two are indistinguishable on any record without a custom
    label, which is most of them, so it is pinned by a test rather than left to inspection.
    """
    word = state_word(record.state, record.orphan_provenance)
    return f"{record.display.rendered} · {word} · {age(record.created_at)}"


class StateGroup(Enum):
    """The four buckets a listed session falls into, in the order a list shows them.

    A `SessionState` is the lifecycle's word; a group is how a *list* arranges those words so
    the owner can scan it -- what is working, what is between states, what needs a hand, and
    what is kept for reading. Both surfaces draw the same four buckets (the bot as headed
    sections, the local surface as a coloured glyph and a count), so the mapping from state to
    bucket lives here once rather than as two dictionaries that agree today (DEC-029, DEC-043).

    Definition order is presentation order and `tuple(StateGroup)` is how a surface walks it.
    """

    ACTIVE = "active"
    IN_TRANSITION = "in_transition"
    NEEDS_ATTENTION = "needs_attention"
    PRESERVED = "preserved"


_GROUP_OF_STATE: dict[SessionState, StateGroup] = {
    SessionState.RUNNING: StateGroup.ACTIVE,
    SessionState.STARTING: StateGroup.IN_TRANSITION,
    SessionState.STOP_REQUESTED: StateGroup.IN_TRANSITION,
    SessionState.FAILED: StateGroup.NEEDS_ATTENTION,
    # Both provenances and the pre-migration `None`: DEC-020 gives the two kinds of ORPHANED
    # different *actions*, and the same place in the list -- an orphan is the row an owner most
    # needs to notice, whichever kind it is.
    SessionState.ORPHANED: StateGroup.NEEDS_ATTENTION,
    SessionState.PRESERVED: StateGroup.PRESERVED,
}
"""ENDED is deliberately absent: `listed_in_sessions` filters it before any grouping happens."""

_EMOJI_OF_GROUP: dict[StateGroup, str] = {
    StateGroup.ACTIVE: "\U0001f7e2",  # 🟢
    StateGroup.IN_TRANSITION: "\U0001f7e1",  # 🟡
    StateGroup.NEEDS_ATTENTION: "\U0001f534",  # 🔴
    StateGroup.PRESERVED: "\u26aa",  # ⚪
}


def state_group(state: SessionState) -> StateGroup | None:
    """Which bucket a state is listed under, or `None` for the one state no list shows.

    Keyed on the state alone and never on provenance, for the reason `listed_in_sessions`
    gives: DEC-020 distinguishes the two kinds of ORPHANED by what can be *done* to them, and
    a list that filed them apart would be inventing a third distinction nobody decided.
    """
    return _GROUP_OF_STATE.get(state)


def group_emoji(group: StateGroup) -> str:
    """The status mark for a whole bucket -- the one every member of it carries."""
    return _EMOJI_OF_GROUP[group]


def state_emoji(state: SessionState) -> str:
    """The status mark for a state, mapped through its group so the two can never disagree.

    Empty for ENDED rather than a fifth mark: nothing lists an ENDED row, and a mark for it
    would be a symbol the owner never learns. A surface that does render one has already made
    the mistake `listed_in_sessions` exists to prevent, and an empty string is the quietest
    honest answer to it.
    """
    group = state_group(state)
    return "" if group is None else group_emoji(group)


def group_counts(records: Iterable[SessionRecord]) -> dict[StateGroup, int]:
    """How many listed sessions sit in each bucket, every bucket present, in list order.

    Every group is a key even at zero, so a surface deciding to *omit* an empty bucket does so
    by reading a zero rather than by noticing an absence -- the two read the same on screen and
    differently in a test. Counted from the records handed in and never from a second read,
    which is the rule `_sessions_reply` already states for its own header.
    """
    counts = dict.fromkeys(StateGroup, 0)
    for record in records:
        group = state_group(record.state)
        if group is not None:
            counts[group] += 1
    return counts


def session_identity(record: SessionRecord) -> str:
    """`project · agent[ · label]` -- the compact name both surfaces put on a two-line row.

    The mode (`regular`, `resumed`, `recovered`) and the sequence are deliberately not here.
    `SessionDisplayIdentity.rendered` carries both because a one-line list needed every token to
    keep siblings apart; the two-line row spends its second line on the state instead and puts
    the sequence in its own weight beside the name, so the surfaces need the name on its own.
    The custom label stays: it is the one token the owner chose. `display.rendered` remains
    the full identity where a screen wants it -- the local surface's breadcrumb, for one.
    """
    display = record.display
    named = f"{display.project_slug} · {display.agent_label}"
    return f"{named} · {display.custom_label}" if display.custom_label else named


ADOPTED_NOTE = "may still be running"
"""What an adopted orphan's row appends, in the words `_ADOPTED_ORPHAN_EXPLANATION` uses.

The strongest honest tense, for the reason that explanation gives: `reconcile` never observes an
ORPHANED record again, so a pane that died weeks ago and one still working read identically.
"""


@dataclass(frozen=True, slots=True)
class SessionRowParts:
    """One listed session, taken apart so a surface can weight and colour each piece.

    `session_lines` joins these into the two plain lines the bot sends; the local surface draws
    each field in its own column and colour and so needs them apart. Both read *this*, which is
    what keeps the two renders one decision: which word, which age, whether a gauge is drawn at
    all, and what an adopted orphan's row confesses are all answered here and nowhere else.
    """

    identity: str
    sequence: int
    group: StateGroup
    state: str
    age: str
    gauge: str | None
    note: str | None


def session_row_parts(
    record: SessionRecord, context: ContextWindow | None = None
) -> SessionRowParts:
    """Take one listed record apart for a two-line or columned row.

    **A gauge is drawn only for a RUNNING session with a known ceiling.** The ceiling rule is
    `context_gauge`'s (a bar with no denominator is DEC-061's forbidden inference drawn instead of
    written); the state rule is new and is the redesign's: a preserved or failed session's context
    is history, and a bar beside `failed` reads as a live figure. A ceilingless reading renders no
    bar here and no count either -- the count belongs to the detail, where `usage_lines` words it.

    Raises on ENDED rather than inventing a group for it, because no list is allowed to hand one
    in (`listed_in_sessions`), and a row quietly filed under "preserved" would hide exactly the
    widening DEC-017 forbids.
    """
    group = state_group(record.state)
    if group is None:
        raise ValueError("an ENDED session has no row; filter with `only_listed` first")
    gauge = None
    if (
        record.state is SessionState.RUNNING
        and context is not None
        and context.used_fraction is not None
    ):
        gauge = context_gauge(context)
    note = (
        ADOPTED_NOTE
        if record.state is SessionState.ORPHANED
        and record.orphan_provenance is OrphanProvenance.ADOPTED
        else None
    )
    return SessionRowParts(
        identity=session_identity(record),
        sequence=record.display.sequence,
        group=group,
        state=state_word(record.state, record.orphan_provenance),
        age=age_short(record.created_at),
        gauge=gauge,
        note=note,
    )


def session_lines(record: SessionRecord, context: ContextWindow | None = None) -> tuple[str, str]:
    """The two-line row: `identity #n` over `state · age[ · gauge][ · note]`.

    Beside `session_row` rather than in place of it, as the redesign's handoff asked: that
    function is pinned as the one-line format and stays importable, this is the two-line one.
    Plain strings, no markup -- the bot bolds the identity and leaves the sequence outside the
    bold, and that is its `escape()`-then-compose business (DEC-014), not this module's.
    """
    parts = session_row_parts(record, context)
    tail = " · ".join(piece for piece in (parts.state, parts.age, parts.gauge, parts.note) if piece)
    return f"{parts.identity} #{parts.sequence}", tail


def with_project_names(
    records: Iterable[SessionRecord], catalogue: Iterable[CatalogProject]
) -> tuple[SessionRecord, ...]:
    """Re-render each record's project under the name the catalogue gives it.

    **The same argument DEC-029 made about a state's name, made about a project's.** What a
    project is *called* is one rule, and it was living in `adapters/telegram/service.py:
    _with_project_name` -- where the local surface could not reach it, and so rendered
    `SessionDisplayIdentity.project_slug` raw. That slug is the catalogue's `opaque_id`, a
    24-character sha256 prefix: a correct key and an unreadable name.

    Promoted rather than copied, and the distinction is the whole reason this function is
    here. A second copy in `adapters/tui/` would have been byte-identical to the bot's on the
    day it was written and answerable to nothing afterwards -- which is precisely BL-031, and
    precisely what this module's own docstring exists to record having ended. The twin is
    caught one step *before* it exists rather than one step after, by
    `tests/unit/application/test_session_views.py:
    test_no_adapter_redefines_the_row_or_the_area_predicate`, whose forbidden tuple names
    both spellings.

    The index is built once per call rather than per record: both callers pass a whole
    listing, and a per-record rebuild would walk a 97-project catalogue once per row.
    *Per call*, and not once per process -- a caller that invokes this on every navigation
    rebuilds it every time. That is deliberate rather than overlooked: the catalogue is
    mutable state on the surface holding it, and a cached index would need invalidating
    wherever a refresh lands. At a 97-project catalogue the rebuild is not measurable; the
    distinction is written down so nobody reads the sentence above as the stronger promise.
    """
    names = {project.opaque_id: project.name for project in catalogue}
    return tuple(
        _with_project_name(record, names.get(str(record.project_id))) for record in records
    )


def _with_project_name(record: SessionRecord, name: str | None) -> SessionRecord:
    """One record under a readable project name, or exactly the record that came in.

    **Three ways to decline, and all three return the record rather than raising.** No name
    in the catalogue (a project deregistered or moved while its session runs); a name the
    slug already carries (nothing to do); and a name `SessionDisplayIdentity` refuses.

    That third one is the one worth stating. The identity demands a non-empty, single,
    printable token, and a catalogue name is a *directory* name -- under no such obligation.
    So `ValueError` here is not a fault, it is the domain declining to render a badly-named
    directory, and the honest answer is the unreadable-but-correct slug. Letting it out would
    take down every session list on both surfaces over one directory with a space in it.
    """
    if name is None or name == record.display.project_slug:
        return record
    try:
        display = replace(record.display, project_slug=name)
    except ValueError:
        return record
    return replace(record, display=display)


def selectable_area(value: str) -> bool:
    """Offer an existing directory only when the project identity rule also accepts it.

    A predicate where the domain answers by raising: a chooser screening candidate
    directories wants a yes or no per row, and `ProjectIdentity` is the authority on which
    ones could ever become a project. Catching `ValueError` alone is deliberate — anything
    else is a fault rather than a refusal, and swallowing it here would take the chooser down
    quietly on the value it was asked to screen.
    """
    try:
        ProjectIdentity(area=value, name=value)
    except ValueError:
        return False
    return True


def listed_in_sessions(record: SessionRecord) -> bool:
    """Whether a session belongs in the list a surface draws. Exactly ENDED does not.

    **The "exactly" is DEC-017's load-bearing word, not emphasis.** That decision keeps force
    stop clearing the record — a row the owner cannot clear was judged the worse failure than
    an over-confident message — and what makes a cleared row acceptable is that every *other*
    state stays visible and actionable. Widen this by one state and the sessions DEC-017
    promises remain reachable are stranded instead, with nothing to say so.

    So the test that guards it enumerates `SessionState` and asserts the whole set, rather
    than naming the states it happens to think of. The record is kept for audit either way;
    what ENDED has lost is anything left to reach, inspect or stop.

    **Provenance is not read, and must not be.** DEC-020 gives the two kinds of ORPHANED
    different *actions*; it gives them the same visibility, and an ORPHANED row is precisely
    the one an owner most needs to see.

    Both surfaces had this as an inline generator expression, and the TUI's carried a comment
    claiming it filtered "exactly as the bot filters it" — true when written, and checked by
    nothing.
    """
    return record.state is not SessionState.ENDED


def only_listed(records: Iterable[SessionRecord]) -> tuple[SessionRecord, ...]:
    """The listable subset, in the order given.

    Order is the caller's: neither surface sorts here, and the row's own age column is what
    tells the owner how old a session is.
    """
    return tuple(record for record in records if listed_in_sessions(record))


class _ListReadableSessions(Protocol):
    """The two questions a list open asks, named so this module needs no use-case import.

    A Protocol rather than `SessionService` because `application/session_views.py` renders and
    filters; importing the service to type one argument would tie a rendering module to the
    one that owns locks and a store. The two frontends pass the same object either way.
    """

    async def refresh_readiness(self) -> object: ...

    async def list_sessions(self) -> Iterable[SessionRecord]: ...


async def listed_sessions(sessions: _ListReadableSessions) -> tuple[SessionRecord, ...]:
    """Refresh readiness, then return the sessions worth showing. The list-open read.

    Both surfaces did exactly this, in exactly this order, and nothing held them together.
    Neither half was the duplicate — `refresh_readiness` was always one method and
    `only_listed` has been shared since Stage 2 — so what was written twice was the *pairing*,
    which is the kind of duplicate no sweep for a repeated name can see.

    **The pairing is the interesting part, because it is deliberately not applied everywhere.**
    The pass rescans every record and runs a tmux capture per FAILED session, so the paths
    that re-read one session — the bot's `_record`, the local surface's `current_record` —
    read without it, and both had written that reasoning down separately. Putting the pair
    here makes "a list open refreshes, a re-read does not" a single decision with one place to
    change it, instead of an agreement between two comments.

    Order is load-bearing and is the reason this is a function rather than two calls a caller
    makes: reading first would draw the list from records the pass is about to promote, so a
    launch that has just become ready would show as FAILED until the next open.

    What happens *after* is the caller's. The local surface hands the result to its console
    (ARCH-B3); the bot does not, because it hosts no console to arrange around a list it has
    just drawn. **Not "the bot has nothing to do with a console"** — it wires a hide-only
    composer of its own (`bootstrap._private_boundary`), so a stop from the phone steps the
    console aside before destroying a pane. That is the correction `application/backend.py`
    already records, and the narrow claim is the one worth repeating here: the asymmetry is in
    what each composer may *do*, not in who has one.
    """
    await sessions.refresh_readiness()
    return only_listed(await sessions.list_sessions())


def usage_lines(usage: AgentUsage | None) -> tuple[str, ...]:
    """Render what a session has spent, or say plainly why there is nothing to render.

    Three outcomes, and keeping them distinct is the whole reason this returns a tuple rather
    than a string:

    - **`None`** — the reader could not match this session to a provider conversation. Usually
      temporary: an agent that has not written its first turn yet has no file to find, so the
      line says *yet* and the owner can reopen the screen.
    - **empty** — the provider matched and publishes nothing. Permanent, and said so, because
      an owner told "not yet" about cursor-agent would wait for a number that is never coming.
    - **anything else** — the context window, and only that.

    **Values, not sentences, since the redesign.** These used to open `Usage: …` / `Context: …`
    and close with a full stop; the bot now renders them as the value of a labelled fact line
    (`context  █████░░░ 62% · 124k / 200k declared`), so the label moved to the surface and
    the wording here is what sits after it. Still plain strings (DEC-043, DEC-014).

    **The plan's rate-limit windows used to render here too, and moving them out is the whole
    of Task 2.1.** They are the account's: both providers that publish one publish it for the
    plan, so the same figure was appearing under whichever session happened to be open and
    reading as that session's spend. `limit_lines` renders them once per agent instead. A
    reading may still *carry* windows — the reader is free to — and this function ignores them.

    A percentage is shown only where the ceiling is *known*, and there are two ways for it to
    be. Codex states it, so its line carries one. Claude records what each turn used and never
    the window it used it out of, so nothing can be derived from the transcript — deriving it
    from the model name would be this function guessing, and it would guess wrong the first time
    the owner switched models mid-session, which Claude Code lets them do. So Claude's line is a
    bare count **unless the owner declares the ceiling in config**, in which case it is used and
    stamped `declared` — see `_context_phrase`. A declared number is configuration, never an
    inference (DEC-061).
    """
    if usage is None:
        return ("no conversation matched yet",)
    if usage.is_empty:
        return ("not reported by this agent",)
    if usage.context is None:
        # Not the sentence above, and the difference is the point. `is_empty` means the provider
        # matched and publishes nothing -- permanent, and worded so. *This* is a reading that
        # carries something (windows, today) but no context, which is a conversation that has
        # not produced a turn yet and will. Collapsing the two told the owner Claude "does not
        # report" in the single most likely moment to open a session detail: launched, prompted,
        # still thinking. `ClaudeUsageReader` no longer produces this shape, but `AgentUsage` is
        # a port type and any reader may, so the distinction is kept where it is rendered rather
        # than left to one adapter's discipline.
        return ("no conversation matched yet",)
    return (_context_phrase(usage.context),)


_STALE_READING_AGE = timedelta(minutes=30)
"""How old an account reading may be before a line says so.

Deliberately the same span `adapters.agents.claude.usage._STALE_LIMIT_AGE` discards a borrowed cache
after. The two do different things with it — that one withholds the figure, this one dates it —
because the cases differ: a borrowed number this project cannot vouch for is better absent,
while a provider's own number stays worth showing as long as it is labelled.
"""


@dataclass(frozen=True, slots=True)
class LimitWindow:
    """One rate-limit window as a surface draws it: the provider's label, a whole percent, and
    how long until it resets (`None` when the provider did not say)."""

    label: str
    percent: int
    resets_in: str | None


@dataclass(frozen=True, slots=True)
class LimitRow:
    """One agent's account-wide windows, taken apart for a surface to lay out.

    `stale_for` is how long ago the figure was read, rendered by `age_short`, and only once that
    is old enough to be news -- `_dated`'s bound. `borrowed` is DEC-061's disclosure, the name
    of the file another program maintains, when the figure came from one.
    """

    profile: str
    windows: tuple[LimitWindow, ...]
    borrowed: str | None
    stale_for: str | None


def limit_rows(limits: Iterable[AgentLimits]) -> tuple[LimitRow, ...]:
    """Each agent's windows as parts, one row per agent that has any -- the decision half.

    `limit_lines` below joins these into the one-line sentence; the redesigned surfaces lay the
    same parts out as a grid (the local surface) and a padded monospace block (the bot), so the
    parts are what both read. Which agents contribute a row, how a percent is rounded, and when a
    reading is old enough to be dated are all decided here once (DEC-043).
    """
    rows = []
    for entry in limits:
        if not entry.windows:
            continue
        rows.append(
            LimitRow(
                profile=str(entry.profile_id),
                windows=tuple(
                    LimitWindow(
                        window.label,
                        round(window.used_percent),
                        None if window.resets_at is None else until(window.resets_at),
                    )
                    for window in entry.windows
                ),
                borrowed=entry.stale_source,
                stale_for=_stale_for(entry.observed_at),
            )
        )
    return tuple(rows)


def limit_lines(limits: Iterable[AgentLimits]) -> tuple[str, ...]:
    """Render each agent's account-wide rate-limit windows, one line per agent that has any.

    The move the owner asked for on 2026-08-29 is mostly this function existing. The same
    windows used to render inside `usage_lines`, under a session's context line, where they
    read as that session's spend — and they never were: both providers that publish a window
    publish it for the whole plan. Naming the agent is what makes the line true, which is why
    `AgentLimits` carries a profile at all.

    **An agent with no windows contributes no line rather than an empty one.** `opencode` and
    `cursor-agent` are permanently in that state and a bare name with nothing after it is
    noise; a Claude whose borrowed cache went stale is temporarily in it, and a line that said
    so would be reporting on this project's plumbing rather than on the owner's plan. The
    surfaces are told the difference by there being nothing to draw.

    Returns strings and nothing else (DEC-043). Whether an empty result means a hidden block,
    a heading over a sentence, or an untouched pane is a layout question, and each adapter
    answers it — as each does its own escaping (DEC-014). Built on `limit_rows`, so the one-line
    form and the laid-out forms cannot disagree about which agents appear or what a percent is.
    """
    lines = []
    for row in limit_rows(limits):
        rendered = " · ".join(_window_phrase_of(window) for window in row.windows)
        borrowed = "" if row.borrowed is None else f" — via {row.borrowed}"
        dated = "" if row.stale_for is None else f" — as of {row.stale_for} ago"
        lines.append(f"{row.profile}: {rendered}{borrowed}{dated}")
    return tuple(lines)


def _window_phrase_of(window: LimitWindow) -> str:
    spent = f"{window.label} {window.percent}%"
    if window.resets_in is None:
        return spent
    return f"{spent} (resets in {window.resets_in})"


def _stale_for(observed_at: datetime | None) -> str | None:
    """How old a reading is, once it is old enough for that to be news; see `_dated`."""
    if observed_at is None or datetime.now(UTC) - observed_at <= _STALE_READING_AGE:
        return None
    return age_short(observed_at)


def _dated(observed_at: datetime | None) -> str:
    """Say how old a reading is, but only once it is old enough for that to be news.

    A rate-limit percentage is frozen the moment its agent stops taking turns, while the window
    it counts against keeps moving — so a figure hours old is a claim about the past rendered in
    the present tense. Codex writes its limits into a rollout and then goes quiet with the
    session, which makes an old reading ordinary rather than exceptional.

    Below the bound nothing is said, because a timestamp on a current number is noise. The
    span matches the one `adapters.agents.claude.usage` fences Claude's borrowed cache
    with, but it is
    a separate constant rather than a shared one — `application` may not import an adapter
    (ARCH-02), and the two do different things with the same number, so `_STALE_READING_AGE`
    states the agreement rather than inheriting it.
    """
    if observed_at is None or datetime.now(UTC) - observed_at <= _STALE_READING_AGE:
        return ""
    return f" — as of {age(observed_at)}"


_GAUGE_CELLS = 8
"""How many cells the bar is drawn from.

Eight, because the gauge sits at the end of a session row that already carries a project, an
agent, a state and an age, and the row has to stay readable in a pane roughly a third of a
narrow terminal wide. Eight cells resolve to 12.5% each, which is finer than the decision the
bar supports -- it answers "roughly how full", and the percent beside it answers exactly.
"""


def percent_gauge(percent: float) -> str:
    """The eight-cell bar for a share already expressed as a percentage, bar only.

    For the account-wide limits, whose figure the provider publishes as a percent and whose
    percent the surface prints beside the bar in its own column. Same cells, same rounding-up,
    same clamp as `context_gauge`, so the two gauges on one screen cannot fill differently for
    the same share.
    """
    return _cells(percent / 100)


def _cells(fraction: float) -> str:
    filled = min(_GAUGE_CELLS, max(0, ceil(fraction * _GAUGE_CELLS)))
    return f"{'█' * filled}{'░' * (_GAUGE_CELLS - filled)}"


def context_gauge(context: ContextWindow) -> str:
    """Render how full one session's context is, as a bar and a share, or as a bare count.

    The owner's sixth ask, and the reason it is a gauge rather than the count the detail screen
    already showed: a list is read by scanning, and `185k` requires knowing the ceiling to mean
    anything while a bar does not.

    **A bar is only drawn where a ceiling is known**, which for Claude means the owner declared
    one and for Codex means the provider published one. Without it this renders the abbreviated
    count `_tokens` already produces and nothing else -- a bar with no denominator would be a
    picture of a number nobody stated, which is the inference DEC-061 forbids, drawn instead of
    written.

    **Over 100% is deliberate and is not clamped.** The Claude ceiling is the owner's
    declaration, so it can be wrong, and a percentage above 100 is impossible for a correct one
    -- which makes it the single loud tell this row can produce. Clamping would delete it and
    leave a wrong ceiling silent in both directions, which is the failure this stage's risk flag
    names. The *bar* is clamped, because a bar wider than its track is a rendering fault rather
    than a signal, and an unclamped one would draw thousands of cells across the row at the
    schema's own floor.

    Returns a string and never an empty one (DEC-043): both surfaces append it to a row they
    have already built, so an empty answer would leave a dangling separator. Placement, and the
    decision to append it at all, stay with each surface -- the TUI appends it and the bot does
    not, because `session_row` is shared and pinned by the parity contract.
    """
    fraction = context.used_fraction
    if fraction is None:
        return _tokens(context.used_tokens)
    # Rounded up (in `_cells`), so any use at all shows one cell: a session that has taken a
    # turn is not in the same state as one that has not, and a bar that reads empty for both
    # hides the difference the gauge exists to show.
    return f"{_cells(fraction)} {round(fraction * 100)}%"


def _context_phrase(context: ContextWindow) -> str:
    """The session's context, saying which denominators this service did not measure.

    The `declared` stamp is DEC-061's disclosure rule reaching the ceiling: Codex publishes its
    window and Claude does not, so one of these percentages is computed against a measurement and
    the other against the owner's statement. Rendered identically they read as equally solid, and
    the owner has no way to tell which number to distrust when a row looks wrong.

    Said here, on the detail line, and not on the row gauge -- the gauge is a glance in a pane a
    third of a narrow terminal wide, and the detail is where a reader goes to check.

    Gauge first, then `used / limit`: the bar is what a reader takes in, the counts are what
    they check it against, and the order follows the reading.
    """
    used = _tokens(context.used_tokens)
    if context.limit_tokens is None or context.used_fraction is None:
        return used
    declared = " declared" if context.limit_declared else ""
    return f"{context_gauge(context)} · {used} / {_tokens(context.limit_tokens)}{declared}"


def _window_phrase(window: UsageWindow) -> str:
    spent = f"{window.label} {_percent(window.used_percent)}"
    if window.resets_at is None:
        return spent
    return f"{spent} (resets in {until(window.resets_at)})"


def _percent(value: float) -> str:
    """Whole percent, because no decision a reader makes here turns on a tenth of one."""
    return f"{round(value)}%"


def _tokens(count: int) -> str:
    """Abbreviate a token count to the precision a reader can actually use.

    One decimal below a hundred thousand and none above it, so `24.3k` keeps the digit that
    distinguishes it from `24.9k` while `185k` does not carry a `.3` nobody reads. Millions get
    one decimal for the same reason `1.0M` and `1.9M` are different answers.
    """
    if count < 1_000:
        return str(count)
    if count < 100_000:
        return f"{count / 1_000:.1f}k"
    if count < 1_000_000:
        return f"{round(count / 1_000)}k"
    return f"{count / 1_000_000:.1f}M"
