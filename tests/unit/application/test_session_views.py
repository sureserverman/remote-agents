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
from remote_agents.application.relative_time import age
from remote_agents.application.session_actions import state_word
from remote_agents.application.session_views import (
    listed_in_sessions,
    listed_sessions,
    only_listed,
    selectable_area,
    session_row,
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
    opaque = "034b69be3a8290521db3d76e"
    record = _record_for(opaque, opaque)
    (named,) = with_project_names((record,), (_catalogued(opaque),))
    assert named.display.project_slug == "remote-agents"
    assert opaque not in named.display.rendered


def test_the_row_key_is_untouched_by_the_naming() -> None:
    """The id is the handle every action screen is reached through; only the *name* moves."""
    opaque = "034b69be3a8290521db3d76e"
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
