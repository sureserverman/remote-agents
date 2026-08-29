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
from dataclasses import replace
from typing import Protocol

from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.relative_time import age, until
from remote_agents.application.session_actions import state_word
from remote_agents.domain.models import SessionRecord, SessionState
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
    - **anything else** — the figures, one line for the context window and one for the plan's
      limit windows, either of which may be absent on its own.

    A percentage is shown only where the provider stated the ceiling. Claude records what each
    turn used and never the window it used it out of, so its line is a bare count; deriving the
    ceiling from the model name would be this function guessing, and it would guess wrong the
    first time the owner switched models mid-session — which is a thing Claude Code lets them
    do.
    """
    if usage is None:
        return ("Usage: no conversation matched yet.",)
    if usage.is_empty:
        return ("Usage: not reported by this agent.",)
    lines = []
    if usage.context is not None:
        lines.append(f"Context: {_context_phrase(usage.context)}")
    elif usage.windows:
        # Reached by Claude and only Claude: its limits are account-wide and come from
        # somewhere else entirely, so they answer while this session's own transcript has not
        # been found — a fresh pane that has not taken a turn yet, or an old session whose
        # conversation has since been cleaned up. Saying so beats a screen that shows `Limits`
        # with no `Context` above it and leaves the reader to work out which half is missing.
        lines.append("Context: no conversation matched yet.")
    if usage.windows:
        rendered = " · ".join(_window_phrase(window) for window in usage.windows)
        borrowed = "" if usage.stale_source is None else f" — via {usage.stale_source}"
        lines.append(f"Limits: {rendered}{borrowed}")
    return tuple(lines)


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
    answers it — as each does its own escaping (DEC-014).
    """
    lines = []
    for entry in limits:
        if not entry.windows:
            continue
        rendered = " · ".join(_window_phrase(window) for window in entry.windows)
        borrowed = "" if entry.stale_source is None else f" — via {entry.stale_source}"
        lines.append(f"{entry.profile_id}: {rendered}{borrowed}")
    return tuple(lines)


def _context_phrase(context: ContextWindow) -> str:
    used = _tokens(context.used_tokens)
    fraction = context.used_fraction
    if context.limit_tokens is None or fraction is None:
        return used
    return f"{used} of {_tokens(context.limit_tokens)} · {round(fraction * 100)}%"


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
