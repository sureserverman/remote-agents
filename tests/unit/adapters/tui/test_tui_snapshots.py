"""A committed visual baseline for every position the local surface can be in.

The rest of this directory asserts *behaviour* — that a key issues a command, that a
rendered row decodes to an action. None of it asserts what the owner actually sees, so a
refactor that drops a state explanation from a detail screen, reorders a confirm's rows so
the destructive one rests under the cursor, or renders an error into a pane nobody displays
passes every one of those tests. This file is the net for that class, and it exists before
the structural refactor that would otherwise spring it silently.

`pytest-textual-snapshot` is deliberately not used: 1.1.0 hard-pins `syrupy==4.8.0`, whose
metadata caps `pytest<9.0.0`, and this project pins `pytest==9.1.1`. Textual's own
`App.export_screenshot()` returns the same SVG the plugin would capture, and it is
deterministic by construction — Rich derives the document's `unique_id` from a `zlib.adler32`
hash of the rendered segments and the title rather than from a random value
(`rich/console.py:2475`), so identical content at an identical size yields a byte-identical
file.

Five things must be pinned for that determinism to hold, and all five are done below.
Three are *environment* dependencies and two are *wall-clock* ones; the distinction matters
because only the latter two flake on a machine that is merely busy, while the former three
flake on a machine that is merely configured differently:

1. **Terminal size** (`_SIZE`), because the SVG encodes pixel geometry — an environment
   dependency, not a clock one: unpinned, every baseline would encode whoever last ran it.
2. **The theme** (`_THEME`). `TEXTUAL_THEME` is read into `constants.DEFAULT_THEME` at import
   time, so a developer who exports it renders every colour differently. Unpinned,
   `TEXTUAL_THEME=textual-light` fails all 26 at once — and the documented remedy for a mass
   failure is to regenerate, which would silently replace the whole net with one person's
   theme.
3. **Colour output.** Rich honours `NO_COLOR` when `export_screenshot` builds its console, so
   that variable alone also fails all 26.
4. **The age column.** `application.relative_time.age()` renders `datetime.now(UTC) -
   created_at`, so every fixture record is stamped at capture time to render a stable
   `0m ago`. That stamping is also why the sub-plan-4 humanization — minutes, then hours,
   then days — moved **no baseline at all**, despite the plan naming it as this
   decomposition's one deliberate re-baseline: a record stamped now is under a minute old
   under either rendering, so the pinning that makes these files deterministic is the same
   thing that makes them blind to the change. A future task that alters how a *young* age
   renders will move all of them at once.

**One hole in this net remains, disclosed because a green suite here reads as "the surface is
unchanged" and for that hole it means nothing of the kind.** Part of sub-plan 4's colour-token
work still moves no baseline: `$foreground` on `#status` is already the default, `$surface` on
the output pane equals the screen's own background, and `$text-muted` reaches only a disabled
empty-state row. `test_status_region.py` carries the assertion instead — it resolves the
rendered colour under two themes that genuinely differ and requires them to differ, which a
hex literal cannot satisfy.

The two gaps that stood beside it are what the state axis below closed, and they are the
reason BL-010 was worth paying for. **Severity is now captured:** measured across all 26
committed baselines, exactly two render an error status (`AREAS_UNREADABLE` and
`SESSIONS_STORE_FAILURE`, at `#b93c5b`), twenty-two render the informational default
(`#e0e0e0`) and the two modals dim theirs (`#646464`) — so deleting the `-error` rule now
moves a file, where before it moved none. `$warning` still reaches no status row in any
capture, because the only warning-severity feedback on these paths is a toast. **And the
empty renders are covered:** `SESSIONS_EMPTY` and `RESUME_PROFILES_NONE_CAPABLE` baseline the
empty-state rows that `test_empty_states.py` used to assert alone.
5. **The input cursor.** A focused `Input` runs a 0.5s wall-clock blink timer
   (`textual/widgets/_input.py:723`), so a capture taken more than half a second after
   focus renders the cursor in the opposite state. `_assert_snapshot` sets `cursor_blink =
   False` on every input first, which pauses that timer and forces the cursor visible
   (`_input.py:527`). Three baselines depend on this — `PROJECTS`, `LABEL` and `NAME` — and
   without it they would pass locally and flake on a loaded machine, which is precisely the
   failure this file exists to prevent rather than reproduce.

Regenerate with `REMOTE_AGENTS_SNAPSHOT_UPDATE=1`, then **read the diff** — an unreviewed
re-baseline turns this file from a net into a rubber stamp.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import Input
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import ALL_SCREENS
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState

_SNAPSHOTS = Path(__file__).parent / "snapshots"
_UPDATE = os.environ.get("REMOTE_AGENTS_SNAPSHOT_UPDATE") == "1"
# Pinned because the SVG encodes pixel geometry: a different terminal size is a different
# file, so an unpinned size would make every baseline depend on whoever last ran it.
_SIZE = (100, 30)
# Pinned for the same reason as the size, and found the same way: `TEXTUAL_THEME` or
# `NO_COLOR` in the environment re-renders every colour in the document, failing all 16
# baselines at once and inviting a regeneration that would bake one person's terminal
# configuration into the net.
_THEME = "textual-dark"

# The sixteen positions, by the name each screen declares and each baseline is committed
# under. A literal tuple rather than a derived one on purpose: deriving it from `ALL_SCREENS`
# would make the suite agree with the code by construction, so a position that lost its
# baseline would stop being compared instead of failing. `test_every_position_has_a_baseline`
# below is what ties this list back to the registry.
_POSITIONS = (
    "PROJECTS",
    "PROFILES",
    "LABEL",
    "REVIEW",
    "AREAS",
    "NAME",
    "PROJECT_REVIEW",
    "SESSIONS",
    "SESSION_DETAIL",
    "FORCE_MODAL",
    "REMOTE_CONTROL_MODAL",
    "INSPECT",
    "RESUME_PROJECTS",
    "RESUME_PROFILES",
    "RESUME_CONVERSATIONS",
    "RESUME_CONFIRM",
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_OTHER = CatalogProject("opaque-other", "other-thing", "dev-area", "Unregistered")
# Fixed rather than minted per run, which `SessionId.new()` did until the state axis below
# needed it. Sixteen position baselines never rendered the raw id — `SessionDetailScreen`
# overwrites the breadcrumb with the record's display identity as soon as it reads one — but
# the SESSION_DETAIL_MISSING state has no record to read, so the bare id stays in the header
# and a freshly minted UUID would write a different SVG on every run.
_SESSION_ID = SessionId.parse("00000000-0000-0000-0000-000000000001")


async def settle(app, pilot, *, tries: int = 20) -> None:
    """Pump until the cursor has been drawn on a row, then let the caller capture.

    `show_choices` schedules a second cursor pass via `call_after_refresh`, so the screen is not
    final the instant the driving coroutine returns. On most screens one `pause()` covers it.
    On AREAS and PROJECT_REVIEW it did not reliably: those two are the only screens whose
    entry path crosses a worker thread (`available_areas`, `creator.create`), and that extra
    hop changes how much work a single pause drains — which showed up as roughly 1 failure in
    6 full-directory runs, on those two screens and no others.

    Waiting on the condition rather than adding another `pause()` or a sleep: the thing being
    waited for is "the highlight has been applied", so that is what this asks about. With
    `OptionList` that is a single reactive on the widget rather than a flag on a mounted row
    per option — `show_choices` sets it synchronously now, so this usually returns on the first
    pause, but it still waits on the real condition and nothing here is wall-clock dependent.

    The two early returns are the screens that legitimately never highlight: a list with no
    rows, and a `show_choices(..., focus=False)` one such as PROJECTS, where the keyboard stays in
    the filter and the list is deliberately left without a cursor.
    """
    from textual.widgets import OptionList

    for _ in range(tries):
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        if not choices.options:
            return
        if choices.highlighted is not None:
            return
        if not choices.has_focus:
            return


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    """A session stamped now, so `age()` renders a stable `0m ago` in the baseline."""
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


# The one reference `_Conversations.catalogue` renders and `resolve_for_resume` resolves.
# Shared so the two fakes cannot drift apart, and so driving RESUME_CONFIRM does not have to
# build a throwaway summary just to read a constant off it.
_REFERENCE = ConversationReference("c-" + "0" * 14 + "01")


def _summary() -> ConversationSummary:
    return ConversationSummary(
        _REFERENCE,
        ProfileId("claude"),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
        description="a saved conversation",
    )


@dataclass(slots=True)
class _Launcher:
    record: SessionRecord = field(default_factory=_record)
    # Three knobs the state axis needs, each defaulting to the happy path so the sixteen
    # position baselines are still driven by the launcher they have always been driven by.
    # `records=()` empties the list; `list_error` makes the store read raise, which is the
    # one and only trigger for `report_store_failure`.
    records: tuple[SessionRecord, ...] | None = None
    list_error: Exception | None = None

    def _listing(self) -> tuple[SessionRecord, ...]:
        return (self.record,) if self.records is None else self.records

    async def refresh_readiness(self):
        return self._listing()

    async def list_sessions(self):
        if self.list_error is not None:
            raise self.list_error
        return self._listing()

    async def copy_attach(self, _session_id):
        return "tmux -L remote-agents attach-session -t =ra-session:"

    async def set_remote_control(self, _command):
        return RemoteControlState.ACTIVE


@dataclass(slots=True)
class _Creator:
    #: Raised out of `available_areas` for the AREAS_UNREADABLE state. The screen reads the
    #: development root on a worker thread, so this is what an unreadable root looks like
    #: from the surface's side of that hop.
    error: Exception | None = None

    def available_areas(self):
        if self.error is not None:
            raise self.error
        return ("dev-area", "infra")


@dataclass(slots=True)
class _Conversations:
    async def catalogue(self, query):
        return ConversationCataloguePage((_summary(),), query.page, 1)

    async def resolve_for_resume(self, reference):
        return ResolvedConversation(_summary(), ProviderConversationId("abc123"))

    async def capabilities(self):
        return (
            ProfileResumeCapability(
                ProfileId("claude"),
                catalogue_available=True,
                selected_resume_available=True,
            ),
        )


def _context(
    *,
    state: SessionState = SessionState.RUNNING,
    launcher: object | None = None,
    creator: object | None = None,
    conversations: object | None = None,
    capture=None,
) -> TuiContext:
    """The collaborators every capture is driven against.

    The four overrides exist for the state axis, and each one defaults to the collaborator
    the position axis has always used — so a state case says exactly which collaborator it
    bends and nothing else, and the sixteen position baselines cannot move because a state
    case needed a different fake.
    """
    launcher = _Launcher(record=_record(state)) if launcher is None else launcher
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=_Creator() if creator is None else creator,  # type: ignore[arg-type]
        profiles=(
            ProfileChoice("claude", True),
            ProfileChoice("codex", False, "not installed on this host"),
        ),
        refresh_catalogue=lambda: (_PROJECT, _OTHER),
        attach_argv=lambda session_id: (
            "tmux",
            "-L",
            "remote-agents",
            "attach-session",
            "-t",
            f"={session_id}",
        ),
        catalogue=(_PROJECT, _OTHER),
        capture=(lambda _session_id: _captured()) if capture is None else capture,
        conversations=_Conversations() if conversations is None else conversations,  # type: ignore[arg-type]
    )


async def _captured() -> str:
    return "the agent said something\nand then something else\n"


def _assert_snapshot(app: RemoteAgentsTui, name: str) -> None:
    """Compare this screen against its committed baseline.

    A missing baseline is a **failure** outside update mode, never a silent write. Writing
    one on demand would make the first run of any new case pass against a file it had just
    produced, which is the one way a snapshot suite can be green and prove nothing.
    """
    # Pin the cursor before capturing. Setting this reactive pauses the blink timer and
    # forces the cursor visible, so a screen with a focused input renders identically
    # whether the capture lands 10ms or 10s after focus.
    for entry in app.screen.query(Input):
        entry.cursor_blink = False
    # A fixed title rather than the app's own: the title is hashed into the SVG's element
    # ids and rendered into its header, so deriving it from app state would couple every
    # baseline to the header's contents.
    svg = app.export_screenshot(title=name)
    path = _SNAPSHOTS / f"{name}.svg"
    if _UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        return
    if not path.exists():
        raise AssertionError(
            f"no baseline for {name!r}. Generate it with "
            f"REMOTE_AGENTS_SNAPSHOT_UPDATE=1 and review the SVG before committing."
        )
    if path.read_text(encoding="utf-8") != svg:
        raise AssertionError(
            f"{name} no longer renders as its baseline. If the change is intended, "
            f"regenerate with REMOTE_AGENTS_SNAPSHOT_UPDATE=1 and review the diff."
        )


async def _drive(app: RemoteAgentsTui, pilot, step: str) -> asyncio.Task[None] | None:
    """Put the app in `step`, using the same private entry points the sibling tests use.

    Answers with the suspended caller when the position is a modal, and with `None`
    otherwise — a modal is not a position the driver can simply arrive at and leave.
    """
    if step == "PROJECTS":
        return None
    if step in {"PROFILES", "LABEL", "REVIEW"}:
        # Through the screens' own handlers, so the baseline captures what the navigation
        # actually builds rather than a screen assembled directly by the test.
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        if step == "PROFILES":
            return None
        await app.screen.choose("claude")
        await pilot.pause()
        if step == "REVIEW":
            app.screen.submit("nightly run")
            await pilot.pause()
        return None
    if step in {"AREAS", "NAME", "PROJECT_REVIEW"}:
        await app.show_areas()
        if step == "AREAS":
            return None
        await app.screen.choose("infra")
        if step == "NAME":
            return None
        app.screen.submit("new-project")
        return None
    if step == "SESSIONS":
        await app.show_sessions()
        return None
    if step in {"SESSION_DETAIL", "FORCE_MODAL", "REMOTE_CONTROL_MODAL", "INSPECT"}:
        await app.show_sessions()
        await app.show_detail(str(_SESSION_ID))
        if step == "FORCE_MODAL":
            # Handed back rather than awaited: a modal suspends the caller that asked until
            # it is answered, and answering it is exactly what would take the screen being
            # captured off screen. The test joins it after the capture.
            return asyncio.create_task(app.screen.confirm_force())
        if step == "REMOTE_CONTROL_MODAL":
            return asyncio.create_task(app.screen.confirm_remote_control(RemoteControlState.ACTIVE))
        elif step == "INSPECT":
            await app.screen.show_inspect()
        return None
    # The four resume positions, each one step further into the same flow.
    await app.action_resume()
    if step == "RESUME_PROJECTS":
        return None
    await app.screen.choose("opaque-existing")
    if step == "RESUME_PROFILES":
        return None
    await app.screen.choose("claude")
    if step == "RESUME_CONVERSATIONS":
        return None
    await app.screen.choose(str(_REFERENCE))
    return None


# --------------------------------------------------------------------------------------
# The second axis: the states a screen enters, rather than the screens themselves.
#
# `_POSITIONS` above is an axis of *screens*, tied to the registry by an equality that
# `test_every_position_has_a_baseline` calls deliberate — a name outliving its screen has to
# fail. The renders below are not screens: the empty list, the unreadable store, the refused
# capture, a detail rendered for a state whose action rows differ. Seven of the ten share a
# position with a baseline already committed under that name, so adding them to `_POSITIONS`
# would break that equality instead of closing a gap. Hence a second axis, with
# `test_every_state_names_a_live_position` as its own tie back to the registry.
#
# What these ten cannot show, stated because a green run here otherwise reads as more than
# it is: `run_test` leaves notifications disabled, so no toast reaches `export_screenshot`.
# Three of the ten put their explanation partly in a toast (the store failure, the
# unreadable root, the attach hand-off), and what is baselined is the part the owner sees on
# the screen itself — the status line, its severity, and the rows that survive. The toast
# text is asserted by `announcements()` in the sibling behaviour tests, and that division is
# why widening this net does not retire those.
# --------------------------------------------------------------------------------------


def _capturing(text: str):
    """A capture port that answers with exactly `text`, however unlikely."""

    async def capture(_session_id) -> str:
        return text

    return capture


class _NoneCapable(_Conversations):
    """A host where every installed agent answers "I cannot resume"."""

    async def capabilities(self):
        return (
            ProfileResumeCapability(
                ProfileId("claude"),
                catalogue_available=False,
                selected_resume_available=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class _State:
    """One render, the name its baseline is committed under, and how to arrive at it.

    `position` is not decoration: it is asserted at capture time, so a state that stops
    being reachable fails where it is named rather than quietly baselining whatever screen
    the drive happened to land on.
    """

    name: str
    position: str
    context: Callable[[], TuiContext]
    drive: Callable[[RemoteAgentsTui, object], Awaitable[None]]


async def _to_sessions(app: RemoteAgentsTui, _pilot) -> None:
    await app.show_sessions()


async def _to_detail(app: RemoteAgentsTui, _pilot) -> None:
    await app.show_sessions()
    await app.show_detail(str(_SESSION_ID))


async def _to_inspect(app: RemoteAgentsTui, pilot) -> None:
    await _to_detail(app, pilot)
    await app.screen.show_inspect()


async def _to_attach(app: RemoteAgentsTui, pilot) -> None:
    await _to_detail(app, pilot)
    await app.screen.show_attach()


async def _to_areas(app: RemoteAgentsTui, _pilot) -> None:
    await app.show_areas()


async def _to_resume_profiles(app: RemoteAgentsTui, pilot) -> None:
    await app.action_resume()
    await pilot.pause()
    await app.screen.choose("opaque-existing")


_STATES = (
    _State(
        "SESSIONS_EMPTY",
        "SESSIONS",
        lambda: _context(launcher=_Launcher(records=())),
        _to_sessions,
    ),
    _State(
        "SESSIONS_STORE_FAILURE",
        "SESSIONS",
        lambda: _context(launcher=_Launcher(list_error=RuntimeError("database is locked"))),
        _to_sessions,
    ),
    _State(
        "SESSION_DETAIL_MISSING",
        "SESSION_DETAIL",
        lambda: _context(launcher=_Launcher(records=())),
        _to_detail,
    ),
    _State(
        "SESSION_DETAIL_PRESERVED",
        "SESSION_DETAIL",
        lambda: _context(state=SessionState.PRESERVED),
        _to_detail,
    ),
    _State(
        "SESSION_DETAIL_STARTING",
        "SESSION_DETAIL",
        lambda: _context(state=SessionState.STARTING),
        _to_detail,
    ),
    _State("SESSION_DETAIL_ATTACH", "SESSION_DETAIL", _context, _to_attach),
    _State(
        "INSPECT_BINARY",
        "INSPECT",
        lambda: _context(capture=_capturing("before\x00after")),
        _to_inspect,
    ),
    _State("INSPECT_EMPTY", "INSPECT", lambda: _context(capture=_capturing("")), _to_inspect),
    _State(
        "AREAS_UNREADABLE",
        "AREAS",
        lambda: _context(creator=_Creator(error=OSError("no such development root"))),
        _to_areas,
    ),
    _State(
        "RESUME_PROFILES_NONE_CAPABLE",
        "RESUME_PROFILES",
        lambda: _context(conversations=_NoneCapable()),
        _to_resume_profiles,
    ),
)


@pytest.fixture(autouse=True)
def _neutral_colour_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render as if no colour-forcing variable were set, whatever the developer exports."""
    for name in ("NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("step", _POSITIONS)
async def test_every_wizard_position_matches_its_baseline(step: str) -> None:
    """Each of the 16 positions renders exactly what its committed baseline shows."""
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=_SIZE) as pilot:
        # Before driving, not at capture time: the theme drives a style recompute, so it has
        # to be set early enough for the pump to have applied it by the time we export.
        app.theme = _THEME
        await pilot.pause()
        asking = await _drive(app, pilot, step)
        await settle(app, pilot)
        assert position(app) == step, f"drove to {position(app)}, expected {step}"
        _assert_snapshot(app, step)
        if asking is not None:
            await pilot.press("escape")
            await asyncio.wait_for(asking, timeout=5)


def test_every_position_has_a_baseline() -> None:
    """The list above covers the registry, so a new position cannot go unwatched.

    This is the tie the parametrization lost when it stopped being derived from an enum.
    A screen added to `ALL_SCREENS` is caught by the back-path suite's own exhaustiveness
    check, but without this it would be silently absent from the *visual* net — which is the
    one defect class this file was bought for, so a hole here is worse than a hole elsewhere.

    Deliberately an equality, not a subset: a name left in `_POSITIONS` after its screen was
    deleted would otherwise sit there pointing at a baseline nothing renders.
    """
    assert set(_POSITIONS) == {screen.position for screen in ALL_SCREENS}


@pytest.mark.parametrize("case", _STATES, ids=lambda case: case.name)
async def test_every_named_state_matches_its_baseline(case: _State) -> None:
    """Each error, empty and alternate-state render matches its committed baseline."""
    app = RemoteAgentsTui(case.context())
    async with app.run_test(size=_SIZE) as pilot:
        app.theme = _THEME
        await pilot.pause()
        await case.drive(app, pilot)
        await settle(app, pilot)
        assert position(app) == case.position, (
            f"{case.name} drove to {position(app)}, expected {case.position}"
        )
        _assert_snapshot(app, case.name)


def test_every_state_names_a_live_position() -> None:
    """This axis's tie back to the registry, and the reason it is a subset not an equality.

    `_POSITIONS` must equal the registry because a screen with no baseline is the hole that
    net exists to close. States are different: nobody claims every screen has an interesting
    second state, so requiring one per screen would invent nine empty cases to satisfy an
    equality. What must hold is the other direction — a state pointing at a position no
    screen declares is a case driving nothing, and it would sit here passing against a
    baseline captured before the screen was deleted.

    The name checks are the same defect one level down: two states sharing a name, or a
    state named for a position, would have the second capture silently overwrite the first
    baseline under `REMOTE_AGENTS_SNAPSHOT_UPDATE=1` and then compare against it forever.
    """
    registry = {screen.position for screen in ALL_SCREENS}
    named = {case.position for case in _STATES}
    assert named <= registry, f"states name positions no screen declares: {named - registry}"

    names = [case.name for case in _STATES]
    assert len(set(names)) == len(names), f"two states share a baseline name: {names}"
    assert not set(names) & set(_POSITIONS), (
        f"a state is named for a position, and would overwrite its baseline: "
        f"{set(names) & set(_POSITIONS)}"
    )


async def test_a_missing_baseline_fails_rather_than_being_written() -> None:
    """The suite cannot go green by generating the file it is about to compare against.

    Without this, adding a case and running it once produces a passing test whose baseline
    nobody ever looked at — a net with a hole exactly where the newest work lands.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=_SIZE) as pilot:
        app.theme = _THEME
        await pilot.pause()
        if _UPDATE:
            pytest.skip("update mode writes baselines by design")
        with pytest.raises(AssertionError, match="no baseline"):
            _assert_snapshot(app, "a-position-that-does-not-exist")
