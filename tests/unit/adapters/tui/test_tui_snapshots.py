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
   `TEXTUAL_THEME=textual-light` fails all 16 at once — and the documented remedy for a mass
   failure is to regenerate, which would silently replace the whole net with one person's
   theme.
3. **Colour output.** Rich honours `NO_COLOR` when `export_screenshot` builds its console, so
   that variable alone also fails all 16.
4. **The age column.** `_age()` renders `datetime.now(UTC) - created_at` in whole minutes,
   so every fixture record is stamped at capture time to render a stable `0m ago`.
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

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import Input

from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
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

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_OTHER = CatalogProject("opaque-other", "other-thing", "dev-area", "Unregistered")
_SESSION_ID = SessionId.new()


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    """A session stamped now, so `_age()` renders a stable `0m ago` in the baseline."""
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

    async def refresh_readiness(self):
        return (self.record,)

    async def list_sessions(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return "tmux -L remote-agents attach-session -t =ra-session:"

    async def set_remote_control(self, _command):
        return RemoteControlState.ACTIVE


@dataclass(slots=True)
class _Creator:
    def available_areas(self):
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


def _context(*, state: SessionState = SessionState.RUNNING) -> TuiContext:
    launcher = _Launcher(record=_record(state))
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
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
        capture=lambda _session_id: _captured(),
        conversations=_Conversations(),  # type: ignore[arg-type]
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
    for entry in app.query(Input):
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


async def _drive(app: RemoteAgentsTui, step: Step) -> None:
    """Put the app in `step`, using the same private entry points the sibling tests use."""
    if step is Step.PROJECTS:
        return
    if step is Step.PROFILES:
        app._choose_project("opaque-existing")
        return
    if step in {Step.LABEL, Step.REVIEW}:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        if step is Step.REVIEW:
            app._submit_label("nightly run")
        return
    if step in {Step.AREAS, Step.NAME, Step.PROJECT_REVIEW}:
        await app._show_areas()
        if step is Step.AREAS:
            return
        await app._choose_area("infra")
        if step is Step.NAME:
            return
        app._submit_name("new-project")
        return
    if step is Step.SESSIONS:
        await app._show_sessions()
        return
    if step in {
        Step.SESSION_DETAIL,
        Step.FORCE_CONFIRM,
        Step.REMOTE_CONTROL_CONFIRM,
        Step.INSPECT,
    }:
        await app._show_sessions()
        await app._show_detail(str(_SESSION_ID))
        if step is Step.FORCE_CONFIRM:
            await app._confirm_force()
        elif step is Step.REMOTE_CONTROL_CONFIRM:
            await app._confirm_remote_control()
        elif step is Step.INSPECT:
            await app._show_inspect()
        return
    # The four resume positions, each one step further into the same flow.
    await app.action_resume()
    if step is Step.RESUME_PROJECTS:
        return
    await app._resolve_resume_project("opaque-existing")
    if step is Step.RESUME_PROFILES:
        return
    await app._resolve_resume_profile("claude")
    if step is Step.RESUME_CONVERSATIONS:
        return
    await app._resolve_resume_conversation(str(_REFERENCE))


@pytest.fixture(autouse=True)
def _neutral_colour_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render as if no colour-forcing variable were set, whatever the developer exports."""
    for name in ("NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("step", list(Step), ids=lambda s: s.name)
async def test_every_wizard_position_matches_its_baseline(step: Step) -> None:
    """Each of the 16 positions renders exactly what its committed baseline shows."""
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=_SIZE) as pilot:
        # Before driving, not at capture time: the theme drives a style recompute, so it has
        # to be set early enough for the pump to have applied it by the time we export.
        app.theme = _THEME
        await pilot.pause()
        await _drive(app, step)
        await pilot.pause()
        assert app._step is step, f"drove to {app._step}, expected {step}"
        _assert_snapshot(app, step.name)


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
