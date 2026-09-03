"""The row format and the area predicate, now that there is one of each.

`adapters/telegram/service.py: _session_row_label` and `adapters/tui/model.py: session_row`
had byte-identical bodies, and each carried a comment naming the other (BL-031). So did
`_selectable_area` and `selectable_area`, docstring included. DEC-029 had already moved
`state_word` into `application/session_actions.py` on exactly this argument — what a state is
*called* belongs to the lifecycle, not to whichever surface draws it — and the rest of the
line simply did not follow it across.

**What these tests are for, given the parity contracts already exist.** Once both surfaces
render from one function, `tests/contract/test_session_row_parity.py` compares that function
with itself and can no longer detect divergence — that is DEC-019's problem and Task 2.4's
job. What it *also* stops doing is pinning the format at all, because a change to the shared
function moves both sides of its assertion together. These tests are where the format is
pinned directly, so the contract file is free to claim only what it checks.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.relative_time import age, age_short
from remote_agents.application.session_actions import state_word
from remote_agents.application.session_views import (
    ADOPTED_NOTE,
    StateGroup,
    _window_phrase,
    context_gauge,
    group_counts,
    group_emoji,
    limit_lines,
    limit_rows,
    listed_in_sessions,
    listed_sessions,
    only_listed,
    percent_gauge,
    selectable_area,
    session_identity,
    session_lines,
    session_row,
    session_row_parts,
    state_emoji,
    state_group,
    with_project_names,
)
from remote_agents.domain.models import (
    OrphanProvenance,
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_usage import AgentLimits, ContextWindow, UsageWindow

_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _record(
    state: SessionState = SessionState.RUNNING,
    provenance: OrphanProvenance | None = None,
    *,
    custom_label: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id=SessionId(UUID(int=1)),
        project_id=ProjectId("demo"),
        profile_id=ProfileId("claude"),
        display=SessionDisplayIdentity("demo", "claude", "regular", 1, custom_label),
        state=state,
        created_at=_NOW - timedelta(minutes=5),
        orphan_provenance=provenance,
    )


# The row ----------------------------------------------------------------------------------


def test_the_row_is_identity_then_state_word_then_age() -> None:
    """The format itself, pinned here rather than in a contract that now compares one
    function with itself."""
    record = _record()

    row = session_row(record)

    # Counted on the *remainder*, not the whole row: `display.rendered` carries separators
    # of its own (`demo · claude · regular · #1`), so a count over the row asserts a number
    # that moves whenever the identity format does — which is a different thing from what
    # this test is about, and was the first version's mistake.
    assert row.startswith(f"{record.display.rendered} · ")
    tail = row.removeprefix(f"{record.display.rendered} · ")
    assert tail.split(" · ") == [state_word(record.state, None), age(record.created_at)]


def test_the_row_reads_the_shared_policy_and_never_state_value() -> None:
    """DEC-029. ORPHANED is the state where the two differ, so it is the one that proves it.

    Both surfaces rendered `state.value` here independently, which is how they showed both
    kinds of ORPHANED identically (BL-031). A row built from `state.value` would pass every
    other assertion in this file.
    """
    adopted = _record(SessionState.ORPHANED, OrphanProvenance.ADOPTED)

    row = session_row(adopted)

    assert SessionState.ORPHANED.value not in row.split(" · ")[1]
    assert state_word(SessionState.ORPHANED, OrphanProvenance.ADOPTED) in row


@pytest.mark.parametrize("state", list(SessionState))
def test_every_state_renders_its_own_word(state: SessionState) -> None:
    record = _record(state)

    assert f" · {state_word(state, None)} · " in session_row(record)


def test_the_three_orphan_cases_stay_distinguishable_in_the_row() -> None:
    """BL-031 was felt in the *list*, so it is the row that has to keep them apart.

    `state_word` has its own test for this; what this adds is that the row actually carries
    the distinction through rather than rendering something upstream of it.
    """
    rows = {
        session_row(_record(SessionState.ORPHANED, provenance))
        for provenance in (OrphanProvenance.ADOPTED, OrphanProvenance.AMBIGUOUS, None)
    }

    assert len(rows) == 3


def test_a_custom_label_reaches_the_row_through_the_identity() -> None:
    """The row renders `display.rendered`, so a renamed session shows its new name.

    Pinned because the row could equally have been built from the generated parts, and the
    two are indistinguishable on every record that has no custom label — which is most of
    the fixtures in this suite.
    """
    assert session_row(_record(custom_label="release review")).startswith(
        "demo · claude · regular · #1 · release review · "
    )


# The two-line row and its parts (the 2026-09-02 redesign) ---------------------------------------


def test_the_two_lines_are_identity_with_sequence_over_state_and_short_age() -> None:
    first, second = session_lines(_record())

    assert first == "demo · claude #1"
    assert (
        second
        == f"{state_word(SessionState.RUNNING, None)} · {age_short(_NOW - timedelta(minutes=5))}"
    )


def test_the_compact_identity_drops_the_mode_and_keeps_the_owners_label() -> None:
    """`display.rendered` carries `regular · #1`; the two-line row spends its second line on the
    state and puts the sequence in its own weight, so the name is project, agent and label."""
    assert session_identity(_record()) == "demo · claude"
    assert (
        session_identity(_record(custom_label="release review")) == "demo · claude · release review"
    )


def test_a_gauge_is_drawn_only_for_a_running_session_with_a_known_ceiling() -> None:
    window = ContextWindow(250_000, 1_000_000)

    assert session_row_parts(_record(), window).gauge == context_gauge(window)
    assert session_row_parts(_record(), ContextWindow(250_000)).gauge is None, "no ceiling"
    assert session_row_parts(_record(SessionState.PRESERVED), window).gauge is None, "not live"
    assert session_row_parts(_record()).gauge is None


def test_an_adopted_orphan_confesses_it_may_still_be_running() -> None:
    adopted = session_row_parts(_record(SessionState.ORPHANED, OrphanProvenance.ADOPTED))
    ambiguous = session_row_parts(_record(SessionState.ORPHANED, OrphanProvenance.AMBIGUOUS))

    assert adopted.note == ADOPTED_NOTE
    assert ambiguous.note is None
    assert session_lines(_record(SessionState.ORPHANED, OrphanProvenance.ADOPTED))[1].endswith(
        f" · {ADOPTED_NOTE}"
    )


def test_the_row_parts_read_the_shared_state_word() -> None:
    assert (
        session_row_parts(_record(SessionState.ORPHANED, OrphanProvenance.ADOPTED)).state
        == "adopted"
    )


def test_an_ended_session_has_no_row_parts() -> None:
    """`only_listed` filters ENDED first; a row for it would be DEC-017's widening in disguise."""
    with pytest.raises(ValueError):
        session_row_parts(_record(SessionState.ENDED))


@pytest.mark.parametrize(
    ("state", "group"),
    [
        (SessionState.RUNNING, StateGroup.ACTIVE),
        (SessionState.STARTING, StateGroup.IN_TRANSITION),
        (SessionState.STOP_REQUESTED, StateGroup.IN_TRANSITION),
        (SessionState.FAILED, StateGroup.NEEDS_ATTENTION),
        (SessionState.ORPHANED, StateGroup.NEEDS_ATTENTION),
        (SessionState.PRESERVED, StateGroup.PRESERVED),
        (SessionState.ENDED, None),
    ],
)
def test_every_state_falls_in_exactly_the_bucket_the_design_names(state, group) -> None:
    assert state_group(state) is group


def test_the_marks_follow_the_buckets() -> None:
    assert state_emoji(SessionState.RUNNING) == "🟢"
    assert state_emoji(SessionState.STARTING) == state_emoji(SessionState.STOP_REQUESTED) == "🟡"
    assert state_emoji(SessionState.FAILED) == state_emoji(SessionState.ORPHANED) == "🔴"
    assert state_emoji(SessionState.PRESERVED) == "⚪"
    assert state_emoji(SessionState.ENDED) == ""
    assert {group_emoji(group) for group in StateGroup} == {"🟢", "🟡", "🔴", "⚪"}


def test_the_buckets_are_walked_in_presentation_order() -> None:
    assert tuple(StateGroup) == (
        StateGroup.ACTIVE,
        StateGroup.IN_TRANSITION,
        StateGroup.NEEDS_ATTENTION,
        StateGroup.PRESERVED,
    )


def test_group_counts_name_every_bucket_even_at_zero() -> None:
    counts = group_counts((_record(), _record(SessionState.FAILED), _record(SessionState.ENDED)))

    assert counts == {
        StateGroup.ACTIVE: 1,
        StateGroup.IN_TRANSITION: 0,
        StateGroup.NEEDS_ATTENTION: 1,
        StateGroup.PRESERVED: 0,
    }


def test_the_percent_gauge_fills_like_the_context_gauge() -> None:
    assert percent_gauge(34) == "███░░░░░"
    assert percent_gauge(0) == "░░░░░░░░"
    assert percent_gauge(100) == "████████"
    assert percent_gauge(25) == context_gauge(ContextWindow(250_000, 1_000_000)).split(" ")[0]


def test_limit_rows_carry_the_parts_the_surfaces_lay_out() -> None:
    resets = datetime.now(UTC) + timedelta(hours=3)
    (row,) = limit_rows(
        (
            _account(
                "codex",
                UsageWindow("5h", 41.4, resets_at=resets),
                UsageWindow("week", 61.0),
                observed=datetime.now(UTC) - timedelta(hours=8),
            ),
        )
    )

    assert row.profile == "codex"
    assert [(w.label, w.percent, w.resets_in) for w in row.windows] == [
        ("5h", 41, "2h"),
        ("week", 61, None),
    ]
    assert row.stale_for == "8h"
    assert row.borrowed is None


def test_limit_lines_are_built_from_the_same_rows() -> None:
    limits = (_account("claude", UsageWindow("5h", 2.0), stale="status-line cache"),)

    (line,) = limit_lines(limits)
    (row,) = limit_rows(limits)

    assert line.startswith(f"{row.profile}: ") and row.borrowed == "status-line cache"


def test_a_short_age_is_the_age_without_its_word() -> None:
    moment = _NOW - timedelta(hours=5)

    assert age(moment) == f"{age_short(moment)} ago"


# The area predicate -----------------------------------------------------------------------


def test_an_area_the_project_identity_rule_accepts_is_selectable() -> None:
    assert selectable_area("infra") is True


@pytest.mark.parametrize("value", ["", " ", "has space", "\x00", "a" * 200, "."])
def test_an_area_the_identity_rule_refuses_is_not_selectable(value: str) -> None:
    """The predicate exists to answer with a bool where the domain answers by raising.

    Parametrized over the refusals rather than asserting one, because a `try/except` that
    caught too broadly — or too narrowly — would pass a single-case test.
    """
    assert selectable_area(value) is False


def test_the_predicate_answers_rather_than_raising() -> None:
    """It swallows `ValueError` and nothing else. A predicate that let another exception
    through would take the project chooser down on a value it was asked to screen."""
    for value in ("", "ok", "..", "x" * 4096):
        assert isinstance(selectable_area(value), bool)


# Neither adapter keeps a copy ---------------------------------------------------------------


def test_no_frontend_decides_for_itself_which_sessions_are_listed() -> None:
    """The ENDED filter's own guard, which the name-based sweep above cannot provide.

    The sweep beside this one forbids *definitions* — `def session_row`, `def
    selectable_area` — because those twins were named functions. **The ENDED filter never
    was.** Both surfaces carried it as an inline generator expression (`if record.state is
    not SessionState.ENDED`), which is the cheapest form to reintroduce and the one form no
    name-based sweep can see: an adapter could grow the comprehension back tomorrow and every
    other test in this file would stay green.

    Found by the Stage 2 gate's evaluator, which noticed the guard covered two of the three
    things the stage merged.

    Scoped to the two frontend trees rather than all of `adapters/`, because
    `adapters/sqlite/session_store.py` legitimately names `SessionState.ENDED` in a
    resume-binding query — that is storage, not a list decision, and a sweep that failed on
    it would be one somebody adds an exemption to and then stops trusting.
    """
    adapters = pathlib.Path(__file__).resolve().parents[3] / "src" / "remote_agents" / "adapters"
    frontends = [adapters / "telegram", adapters / "tui"]
    for tree in frontends:
        assert tree.is_dir(), f"{tree} must exist or this sweep passes over nothing"

    offenders = sorted(
        path.relative_to(adapters).as_posix()
        for tree in frontends
        for path in tree.rglob("*.py")
        if "SessionState.ENDED" in path.read_text("utf-8")
    )
    assert offenders == [], (
        "a frontend named ENDED itself; which sessions are listed is `listed_in_sessions`'s "
        "decision and DEC-017's argument depends on there being exactly one of it"
    )


def test_no_adapter_redefines_the_row_or_the_area_predicate() -> None:
    """The Stage 2 gate's own sweep, as a test, for the reason Stage 1's sweep became one.

    Byte-identical twins are what BL-031 was: two definitions that agreed on the day they
    were written and had no mechanism keeping them agreeing. A gate check that runs once
    cannot see the day one of them is edited.
    """
    adapters = pathlib.Path(__file__).resolve().parents[3] / "src" / "remote_agents" / "adapters"
    assert adapters.is_dir(), "the sweep must fail loudly rather than pass over nothing"

    forbidden = (
        "def session_row",
        "def _session_row_label",
        "def selectable_area",
        "def _selectable_area",
        # The naming rule joined the sweep the day it was promoted, not the day a second
        # surface wanted it. `_with_project_name` lived in the bot alone and was *about* to
        # be copied into the local surface -- the manufactured-twin shape BL-031 records,
        # caught one step before it existed rather than one step after.
        "def _with_project_name",
        "def with_project_name",
        "def with_project_names",
        # The 2026-09-02 redesign's helpers, added the day they were written rather than the
        # day a second surface wanted them: the two-line row and its parts, the compact
        # identity, the four buckets and their marks, and the short age.
        "def session_lines",
        "def session_row_parts",
        "def session_identity",
        "def state_group",
        "def state_emoji",
        "def group_emoji",
        "def group_counts",
        "def age_short",
    )
    offenders = {
        path.relative_to(adapters).as_posix(): sorted(found)
        for path, found in (
            (path, {name for name in forbidden if name in path.read_text("utf-8")})
            for path in sorted(adapters.rglob("*.py"))
        )
        if found
    }
    assert offenders == {}


# Which sessions a list shows -----------------------------------------------------------------


def test_exactly_ended_is_filtered_from_the_list() -> None:
    """DEC-017's argument turns on *only* ENDED being hidden, so the whole set is asserted.

    That decision keeps force stop clearing the record — a row the owner cannot clear being
    judged the worse failure — and the reason a cleared row is acceptable is that every other
    state stays visible and actionable. Widening this predicate by one state would strand
    exactly the sessions DEC-017 promises remain reachable, and would do it silently: a test
    naming two or three states would still pass.

    Both surfaces had this as an inline generator expression, and the TUI's carried a comment
    asserting it filtered "exactly as the bot filters it" — a claim nothing checked.
    """
    listed = {state for state in SessionState if listed_in_sessions(_record(state))}

    assert listed == set(SessionState) - {SessionState.ENDED}


@pytest.mark.parametrize("state", list(SessionState))
def test_the_predicate_answers_for_every_state_and_reads_only_the_state(
    state: SessionState,
) -> None:
    """Provenance must not reach this decision. ORPHANED is visible whichever kind it is —
    DEC-020 gives the two branches different *actions*, never different visibility."""
    answers = {
        listed_in_sessions(_record(state, provenance))
        for provenance in (OrphanProvenance.ADOPTED, OrphanProvenance.AMBIGUOUS, None)
    }

    assert len(answers) == 1, "provenance changed whether the row is listed, and must not"


def test_filtering_a_batch_keeps_order_and_drops_only_the_ended() -> None:
    records = tuple(_record(state) for state in SessionState)

    kept = only_listed(records)

    assert [r.state for r in kept] == [s for s in SessionState if s is not SessionState.ENDED]


class _RecordingSessions:
    """Answers a list open while writing down what it was asked, and in which order."""

    def __init__(self, records: tuple[SessionRecord, ...]) -> None:
        self._records = records
        self.asked: list[str] = []

    async def refresh_readiness(self) -> None:
        self.asked.append("refresh")

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        self.asked.append("read")
        return self._records


@pytest.mark.asyncio
async def test_a_list_open_refreshes_before_it_reads() -> None:
    """The order is the reason this is one function rather than two calls at each surface.

    Reading first would draw the list from records the pass is about to promote, so a launch
    that had just become ready would show as FAILED until the owner opened the list again —
    a stale row that corrects itself, which is the hardest kind to notice or report.
    """
    sessions = _RecordingSessions((_record(SessionState.RUNNING),))

    await listed_sessions(sessions)

    assert sessions.asked == ["refresh", "read"]


@pytest.mark.asyncio
async def test_a_list_open_returns_only_what_a_list_may_show() -> None:
    """The read half is `only_listed`, so DEC-017's "exactly ENDED" reaches both surfaces."""
    running = _record(SessionState.RUNNING)
    ended = _record(SessionState.ENDED)
    sessions = _RecordingSessions((running, ended))

    assert await listed_sessions(sessions) == (running,)


@pytest.mark.asyncio
async def test_a_list_open_keeps_the_order_the_store_gave() -> None:
    """Neither surface sorts here; the row's age column is what tells the owner how old it is."""
    first = _record(SessionState.RUNNING)
    second = _record(SessionState.PRESERVED)
    sessions = _RecordingSessions((second, first))

    assert await listed_sessions(sessions) == (second, first)


# Which name a row shows for its project -------------------------------------------------------


def _catalogued(opaque_id: str, name: str = "remote-agents") -> CatalogProject:
    return CatalogProject(opaque_id, name, "infra", "Registered")


def _record_for(project_id: str, slug: str) -> SessionRecord:
    """A record whose `project_slug` is whatever the store persisted for it.

    The store persists the *slug*, and for a catalogue project that slug is the opaque id --
    a 24-character sha256 prefix. That is the whole defect: it is a correct, stable key and
    an unreadable name, and only the catalogue can turn one into the other.
    """
    return SessionRecord(
        session_id=SessionId(UUID(int=2)),
        project_id=ProjectId(project_id),
        profile_id=ProfileId("claude"),
        display=SessionDisplayIdentity(slug, "claude", "regular", 3),
        state=SessionState.RUNNING,
        created_at=_NOW - timedelta(minutes=5),
    )


def test_a_slug_that_is_a_catalogue_id_renders_the_catalogue_name() -> None:
    opaque = "000000000000000000000000"
    record = _record_for(opaque, opaque)
    (named,) = with_project_names((record,), (_catalogued(opaque),))
    assert named.display.project_slug == "remote-agents"
    assert opaque not in named.display.rendered


def test_the_row_key_is_untouched_by_the_naming() -> None:
    """The id is the handle every action screen is reached through; only the *name* moves."""
    opaque = "000000000000000000000000"
    record = _record_for(opaque, opaque)
    (named,) = with_project_names((record,), (_catalogued(opaque),))
    assert named.session_id == record.session_id
    assert named.project_id == record.project_id
    assert named.state is record.state


def test_a_project_absent_from_the_catalogue_is_left_alone() -> None:
    """Unchanged, never blanked. A project can leave the catalogue while its session runs --
    deregistered, or a directory moved -- and the slug is then the only name there is."""
    record = _record_for("gone", "gone")
    (named,) = with_project_names((record,), (_catalogued("other"),))
    assert named is record


def test_a_slug_already_equal_to_the_name_is_left_alone() -> None:
    record = _record_for("plain", "plain")
    (named,) = with_project_names((record,), (_catalogued("plain", "plain"),))
    assert named is record


@pytest.mark.parametrize(
    "rejected",
    ("", "  ", "my project", "tab\there", "bell\x07"),
    ids=("empty", "whitespace-only", "inner-space", "tab", "unprintable"),
)
def test_a_name_the_identity_would_reject_leaves_the_record_unchanged(rejected: str) -> None:
    """`SessionDisplayIdentity` demands a single printable token, and a catalogue name is a
    directory name -- which is under no such obligation. Raising here would take down every
    list on both surfaces over one badly-named directory, so the unreadable-but-correct slug
    is kept and the row still renders."""
    record = _record_for("id", "id")
    (named,) = with_project_names((record,), (_catalogued("id", rejected),))
    assert named is record


def test_every_record_is_returned_even_when_none_can_be_named() -> None:
    """The count is the contract: a surface fills its list from this and a dropped record is
    a session the owner cannot reach."""
    records = tuple(_record_for(f"p{index}", f"p{index}") for index in range(4))
    assert len(with_project_names(records, ())) == len(records)


def test_an_empty_catalogue_returns_the_records_unchanged() -> None:
    records = (_record_for("a", "a"), _record_for("b", "b"))
    assert with_project_names(records, ()) == records


# --- the account-wide limits block --------------------------------------------------------


def _account(
    profile: str, *windows, stale: str | None = None, observed: datetime | None = None
) -> AgentLimits:
    return AgentLimits(ProfileId(profile), tuple(windows), observed_at=observed, stale_source=stale)


def test_one_line_per_answering_agent_named_by_its_profile() -> None:
    """The name is the whole point of moving the block: it says whose plan this is."""
    lines = limit_lines(
        (
            _account("claude", UsageWindow("5h", 2.0), UsageWindow("week", 88.0)),
            _account("codex", UsageWindow("5h", 41.0)),
        )
    )

    assert lines == ("claude: 5h 2% · week 88%", "codex: 5h 41%")


def test_a_borrowed_figure_says_where_it_came_from() -> None:
    """DEC-061: a number read out of a file this project does not own is stamped as such."""
    (line,) = limit_lines((_account("claude", UsageWindow("5h", 2.0), stale="status-line cache"),))

    assert line == "claude: 5h 2% — via status-line cache"


def test_a_figure_from_the_providers_own_accounting_carries_no_stamp() -> None:
    """Codex writes its own rate limits into its own rollout, so there is nothing to disclose."""
    (line,) = limit_lines((_account("codex", UsageWindow("5h", 41.0)),))

    assert "via" not in line


def test_the_window_phrase_is_the_one_the_session_line_already_used() -> None:
    """DEC-043 reached from the other side: one decision about how a window reads, not two.

    A reset is worded by `_window_phrase`, so the account block and a session's own line
    cannot start disagreeing about what `resets in` means.
    """
    resets = datetime.now(UTC) + timedelta(hours=3)

    (line,) = limit_lines((_account("codex", UsageWindow("5h", 41.0, resets_at=resets)),))

    assert line == f"codex: {_window_phrase(UsageWindow('5h', 41.0, resets_at=resets))}"


def test_an_agent_that_published_nothing_contributes_no_line() -> None:
    """opencode and cursor-agent are always this, and a bare name with no figure is noise."""
    assert limit_lines((_account("opencode"), _account("cursor-agent"))) == ()


def test_an_account_nothing_answered_for_renders_an_empty_block() -> None:
    """Not a placeholder sentence: the surfaces decide what an absent block looks like.

    DEC-043 -- this returns strings and takes no view on whether a screen shows a heading
    over nothing, which is a layout question and stays with each adapter.
    """
    assert limit_lines(()) == ()
    assert limit_lines((_account("claude"), _account("codex"))) == ()


def test_a_reading_older_than_the_bound_says_how_old_it_is() -> None:
    """A percentage only moves when the agent takes a turn; the window moves regardless.

    So an hours-old figure is not a slightly-old truth, and rendering it bare would present a
    stale number as the present one. Codex writes its rate limits into a rollout and then goes
    quiet with the session, which makes this the ordinary case rather than an edge.
    """
    (line,) = limit_lines(
        (
            _account(
                "codex",
                UsageWindow("week", 61.0),
                observed=datetime.now(UTC) - timedelta(hours=8),
            ),
        )
    )

    assert line == "codex: week 61% — as of 8h ago"


def test_a_fresh_reading_is_not_dated() -> None:
    """Below the bound the figure is current, and a timestamp on it would be noise."""
    (line,) = limit_lines(
        (_account("codex", UsageWindow("week", 61.0), observed=datetime.now(UTC)),)
    )

    assert line == "codex: week 61%"


def test_a_reading_that_states_no_observation_time_is_not_dated() -> None:
    """`observed_at` is optional, and an absent one is not evidence of staleness."""
    (line,) = limit_lines((_account("codex", UsageWindow("week", 61.0)),))

    assert "as of" not in line


def test_a_window_that_has_already_reset_reads_as_zero_rather_than_negative() -> None:
    """Carried over from the session line this block replaced, because the clamp still matters.

    `until` refuses a negative remainder: a clock the reader cannot see disagreeing by a second
    should read `0m`, not `-1m`, which looks like a broken session rather than a slow clock.
    """
    (line,) = limit_lines((_account("codex", UsageWindow("5h", 0.0, resets_at=datetime.now(UTC))),))

    assert line == "codex: 5h 0% (resets in 0m)"


# --- the per-row context gauge ------------------------------------------------------------


def test_a_known_ceiling_renders_a_bar_and_a_whole_percent() -> None:
    """A share is the readable form, and the bar is what makes it readable at a glance."""
    gauge = context_gauge(ContextWindow(250_000, 1_000_000))

    assert "25%" in gauge
    assert gauge.count("█") == 2 and gauge.count("░") == 6


@pytest.mark.parametrize(
    ("used", "limit", "expected"),
    [(0, 1_000_000, "0%"), (1_000_000, 1_000_000, "100%"), (999_999, 1_000_000, "100%")],
)
def test_the_percent_spans_its_whole_range(used: int, limit: int, expected: str) -> None:
    assert expected in context_gauge(ContextWindow(used, limit))


def test_a_ceilingless_reading_renders_the_count_it_already_had() -> None:
    """No ceiling, no share -- and the abbreviated count `_tokens` already produces, not a bar.

    A bar with no denominator would be a picture of a number nobody stated, which is the
    inference DEC-061 exists to forbid rendered as a graphic.
    """
    assert context_gauge(ContextWindow(24_349)) == "24.3k"


def test_nothing_ever_renders_an_empty_string() -> None:
    """The surfaces append this to a row, so an empty answer would leave a dangling separator."""
    for context in (ContextWindow(0), ContextWindow(0, 1_000_000), ContextWindow(1)):
        assert context_gauge(context).strip()


def test_the_gauge_is_never_folded_into_the_shared_row() -> None:
    """`tests/contract/test_session_row_parity.py` pins that both surfaces resolve to one
    `session_row`, so a gauge added there would appear on the bot's rows too -- which the owner
    asked for on the TUI alone. The TUI appends it at its own draw sites instead."""
    record = _record()

    assert "%" not in session_row(record)
    assert "█" not in session_row(record)


def test_a_session_that_has_taken_a_turn_never_reads_as_an_empty_bar() -> None:
    """Rounded up, so the first cell lights the moment there is anything to show.

    A session that has taken one turn is not in the same state as one that has taken none, and
    a bar that floors renders both as empty -- hiding exactly the difference a glance-readable
    gauge exists to show. Pinned because the rounding is otherwise invisible: flooring passed
    every other check in this file.
    """
    barely = context_gauge(ContextWindow(1, 1_000_000))
    untouched = context_gauge(ContextWindow(0, 1_000_000))

    assert barely.startswith("█")
    assert untouched.startswith("░")
    assert barely != untouched


@pytest.mark.parametrize(
    ("used", "limit", "percent", "cells"),
    [(400_000, 200_000, "200%", 8), (2_000_000, 200_000, "1000%", 8), (10**6, 1_000, "100000%", 8)],
)
def test_a_session_past_a_wrong_ceiling_says_so_instead_of_clamping(
    used: int, limit: int, percent: str, cells: int
) -> None:
    """The one loud tell a row can produce, and the reason the percent is not clamped.

    The Claude ceiling is the owner's declaration, so it can be wrong. Above 100% is impossible
    for a correct one, so the row announces an understated ceiling by itself -- clamping to 100%
    would delete that and leave a wrong ceiling silent in both directions, which is the risk
    this stage was flagged for. Pinned as intended behaviour rather than left as an
    unconsidered path.
    """
    gauge = context_gauge(ContextWindow(used, limit))

    assert percent in gauge
    assert gauge.count("█") == cells and gauge.count("░") == 0


def test_the_bar_is_clamped_even_though_the_percent_is_not() -> None:
    """The bar has a track and the number does not, so only one of them may overflow.

    Without the clamp the fill count grows with the fraction and the empty remainder goes
    negative -- rendering an 8000-cell bar across one row at the schema's own floor. That guard
    had no test: every case constructed here previously topped out at exactly the ceiling, where
    `min` is a no-op.
    """
    gauge = context_gauge(ContextWindow(1_000_000, 1_000))

    assert len(gauge.split(" ")[0]) == 8
