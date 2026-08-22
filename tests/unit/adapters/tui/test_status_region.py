"""The status region is one line, and the other two sinks carry what no longer fits.

The defect this pins is a *layout* one as much as a wording one: `#status` was `height: auto`,
so the rows underneath it moved whenever a one-line instruction was replaced by a four-line
failure. Everything here is about keeping the three sinks separate — the header's breadcrumb,
the one-line status, and the toast — because they were one region doing three jobs and each
was worse for it.

Four guards on the one-line contract, because it can be broken in four places and no single
check sees them all:

* `test_a_long_status_does_not_move_the_rows_beneath_it` — the layout itself.
* `test_no_call_site_writes_a_multi_line_status` — literals, read out of the source. This is
  the one that fails on a *new* multi-line call before anybody runs the screen it is on.
* `test_a_screen_cannot_declare_a_multi_line_default` — the class-level `status`, which
  reaches the widget without passing through `set_status`.
* `test_a_multi_line_status_is_truncated_rather_than_silently_clipped` — values a static
  check cannot see, which is every exception this surface interpolates.

Then the other two sinks: that a failure reaches the toast rather than the status line and
renders untrusted text literally there, and that the header's trail is right at every position
on the way into a flow rather than only at the end of one.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from backends import tui_context_for
from textual.widgets import OptionList, Static

# Neither is re-exported from `textual.widgets`. Both are system widgets this app never
# constructs itself — the toast is raised by `notify`, the header title is composed inside
# `Header` — so reaching past the package boundary is the only way to assert on either.
from textual.widgets._header import HeaderTitle as _HeaderTitle
from textual.widgets._toast import Toast as _Toast
from tui_feedback import announcements, breadcrumb
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.screens import ALL_SCREENS
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.profiles import ProfileAvailability
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
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState

_SURFACE = Path(RemoteAgentsTui.__module__.replace(".", "/")).parent
_SURFACE_ROOT = Path(__file__).resolve().parents[4] / "src" / _SURFACE

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")
#: An unbalanced bracket and a live link directive, the same pair `test_row_markup` uses.
#: Console markup raises `MarkupError` on the first and acts on the second.
_MARKUP = "boom [unclosed and [link=file:///etc/passwd]this[/link]"


@dataclass(slots=True)
class _FakeRecord:
    # Mirrors SessionRecord's tenth field. A fake missing it duck-types the record
    # everywhere except the one branch DEC-020 added, which is the branch that offers a
    # destructive action.
    orphan_provenance = None

    session_id: SessionId
    state: SessionState


class _FakeLauncher:
    """Report the state the test asked for, or raise the error it was given."""

    def __init__(self, state: SessionState = SessionState.RUNNING, error: Exception | None = None):
        self.state = state
        self.error = error

    async def launch(self, command: LaunchCommand) -> _FakeRecord:
        if self.error is not None:
            raise self.error
        return _FakeRecord(SessionId.new(), self.state)


class _FakeCreator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")


_REFERENCE = ConversationReference("c-0000000000000001")


def _summary() -> ConversationSummary:
    return ConversationSummary(
        _REFERENCE,
        ProfileId("claude"),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
        description="a saved conversation",
    )


class _Conversations:
    """The smallest resume provider that reaches the confirmation, for the trail test."""

    async def capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)

    async def catalogue(self, query) -> ConversationCataloguePage:
        return ConversationCataloguePage((_summary(),), query.page, 1)

    async def resolve_for_resume(self, reference: ConversationReference):
        if reference != _REFERENCE:
            return None
        return ResolvedConversation(_summary(), ProviderConversationId("abc123def456"))


def _context(**overrides: object) -> TuiContext:
    arguments: dict[str, object] = {
        "sessions": _FakeLauncher(),
        "projects": _FakeCreator(),
        "profiles": (ProfileAvailability("claude", True),),
        "refresh_catalogue": lambda: (_EXISTING,),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_EXISTING,),
    }
    arguments.update(overrides)
    return tui_context_for(**arguments)


async def _walk_to_review(app: RemoteAgentsTui, pilot) -> None:
    """Two choices, not three: the agent choice is the arrival at the review."""
    await app.screen.choose("opaque-existing")
    await pilot.pause()
    await app.screen.choose("launch")
    await pilot.pause()
    await app.screen.choose("claude")
    await pilot.pause()


# The layout ---------------------------------------------------------------------


async def test_the_attach_command_renders_whole_at_eighty_columns() -> None:
    """The one payload here the owner has to *copy*, measured against the region that holds it.

    A gate evaluator found this by driving the real `attach_argv`: `Attach with: tmux -L
    remote-agents attach-session -t ra-<uuid>:` is 93 characters, and a one-row `nowrap` region
    ellipsised it mid-UUID at 80 columns — Textual's own default width. A terminal can only
    copy what it draws, so a cut command is not a shortened command, it is no command.

    Three things this test does that the suite could not before, each of which is why the
    defect survived: it uses the **production** argv shape rather than the short fake every
    other fixture builds, it reads the **rendered** line rather than `Static.content` (which
    holds the untruncated value and would pass either way), and it renders at **80** rather
    than the 100 the snapshot baselines are committed at, where the string happens to fit.
    """
    session = "ra-5175f1d9-7f45-4981-83e9-158923e33000:"
    command = " ".join(("tmux", "-L", "remote-agents", "attach-session", "-t", session))
    app = RemoteAgentsTui(_context())

    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        status = app.screen.query_one("#status", Static)
        app.screen.set_status(f"Attach with: {command}")
        await pilot.pause()
        drawn = "".join(status.render_line(row).text for row in range(status.size.height))

    # Compared with whitespace removed from both sides, because the assertion is about which
    # *characters reached the screen*, not about where the wrap fell. Reassembling the rows
    # with a separator would be a guess about the wrap point, and guessing it wrong fails a
    # command that is fully drawn — the one outcome this test must not produce.
    assert "…" not in drawn, f"the attach command was elided at 80 columns: {drawn!r}"
    assert "".join(command.split()) in "".join(drawn.split()), (
        f"the command is not on screen in full, so it cannot be copied: {drawn!r}"
    )


async def test_a_long_status_does_not_move_the_rows_beneath_it() -> None:
    """The reflow, asserted directly rather than through anything that produces one.

    A status four lines long and a status one line long must leave `#choices` in the same
    place. Driven by writing straight to `set_status` because the point is the *region*, not
    any particular caller: this stays true however the call sites are later reworded, and it
    fails the moment `height: 1` is relaxed back to `auto`.
    """
    app = RemoteAgentsTui(_context())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        rows = app.screen.query_one("#choices", OptionList)
        before = rows.region

        app.screen.set_status("one line")
        await pilot.pause()
        one_line = rows.region

        app.screen.set_status("first\nsecond\nthird\nfourth")
        await pilot.pause()
        four_lines = rows.region
        height = app.screen.query_one("#status", Static).region.height

    assert one_line == four_lines == before, (
        "the rows moved when the status grew, which is the reflow the region split fixes"
    )
    # Two rows, and *fixed* is the property that matters — the rows beneath never move. The
    # second row is there so one long logical line wraps instead of being cut; it is not a
    # licence for a second sentence, which `test_no_call_site_writes_a_multi_line_status` and
    # `set_status`'s own guard still refuse.
    assert height == 2, f"the status region is {height} rows high, not 2"


# The literals -------------------------------------------------------------------


def _set_status_arguments() -> list[tuple[str, int, ast.expr]]:
    """Every `.set_status(...)` first argument in the surface package, with where it is."""
    found: list[tuple[str, int, ast.expr]] = []
    for path in sorted(_SURFACE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_status"
                and node.args
            ):
                found.append((path.name, node.lineno, node.args[0]))
    return found


def _literal_parts(expression: ast.expr) -> list[str]:
    """The parts of `expression` whose text is fixed — a constant, or an f-string's spans."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return [expression.value]
    if isinstance(expression, ast.JoinedStr):
        return [
            part.value
            for part in expression.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
    return []


def test_the_source_sweep_finds_the_call_sites_it_claims_to() -> None:
    """The sweep below is only worth what it reads; a broken path would pass it silently.

    A check that walks a directory and finds nothing reports the same green as one that
    walked it and found everything in order. This is the difference, asserted rather than
    assumed — and it is the reason the sweep names a floor rather than an exact count, which
    would turn every new call site into a failure of this test instead of the next one.
    """
    arguments = _set_status_arguments()
    assert len(arguments) >= 20, f"only {len(arguments)} call sites found under {_SURFACE_ROOT}"


def test_no_call_site_writes_a_multi_line_status() -> None:
    """One line is the contract; this is where a new second line is caught.

    Literals only, deliberately. An interpolated value can carry a newline and no static
    check can see it — that is what the runtime guard below is for, and saying so here keeps
    this test from being read as proving more than it does.
    """
    offenders = [
        f"{name}:{line}"
        for name, line, argument in _set_status_arguments()
        for part in _literal_parts(argument)
        if "\n" in part
    ]
    assert offenders == [], f"multi-line status literals at {offenders}"


@pytest.mark.parametrize("screen_type", ALL_SCREENS, ids=lambda c: c.__name__)
def test_a_screen_cannot_declare_a_multi_line_default(screen_type: type) -> None:
    """The class-level `status` is written to the widget by `on_mount`, not by `set_status`."""
    assert "\n" not in getattr(screen_type, "status", "")


def test_declaring_a_multi_line_default_is_refused_at_class_creation() -> None:
    """The guard above only holds because defining such a screen is impossible.

    Without this, `test_a_screen_cannot_declare_a_multi_line_default` is a test that agrees
    with the code by construction — it can only report what the existing screens happen to
    declare, and would pass on a codebase where the rule was never enforced at all.
    """
    with pytest.raises(ValueError, match="one line"):

        class _TwoLines(ChoiceScreen):
            status = "first\nsecond"


async def test_a_multi_line_status_is_truncated_rather_than_silently_clipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception's text carries newlines the static sweep above cannot see.

    Rendering the first line and logging the whole value is the honest failure: the region is
    one line high, so a second line is not shortened, it is *absent*, and a loss nobody can
    find is worse than one in the log.
    """
    app = RemoteAgentsTui(_context())

    with caplog.at_level(logging.WARNING):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.set_status("visible\nswallowed")
            await pilot.pause()
            shown = _status(app)

    assert shown == "visible"
    assert any("swallowed" in record.getMessage() for record in caplog.records), (
        "the discarded line was not logged, so it is lost with nobody told"
    )


# The toast, which is a new sink for text this app does not author ---------------


async def test_a_failed_launch_is_an_error_notification_rather_than_a_four_line_status() -> None:
    """The task's own case: a failure is announced, and the position keeps its own line.

    Both halves are asserted. Only checking the toast would pass on a surface that also left
    the four-line failure in the status region, which is the state this task started from.
    """
    launcher = _FakeLauncher(error=RuntimeError("the terminal port broke its contract"))
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _walk_to_review(app, pilot)
        await app.screen.choose("launch")
        await pilot.pause()
        errors = announcements(app, severity="error")
        status = _status(app)
        choices = app.screen.query_one("#choices", OptionList)
        rows = [str(option.prompt) for option in choices.options]

    assert any("broke its contract" in message for message in errors), errors
    assert "\n" not in status
    assert "broke its contract" not in status
    assert rows[:1] == ["Launch"], "the review kept its rows, so the failure is retryable"


async def test_a_notification_shows_markup_bearing_text_literally() -> None:
    """The fourth sink for text this app did not write, and the third to need saying so.

    `#status` and `#choices` are `markup=False` because the owner's label, the agent's echoed
    description and the raw captured output all reach them. Every one of those now also
    reaches a toast — an exception message interpolates the first two directly — and `Toast`
    renders console markup by default, where an unbalanced `[` raises `MarkupError`.

    Asserted by rendering the toast itself rather than the notification's `markup` flag: the
    flag is the mechanism and this is the property, and `Toast.render` is where the two meet —
    it calls `Content.from_markup` on the message when the flag is set and `Content` when it
    is not, so a regression here does not return the wrong string, it *raises* on this input.
    """
    launcher = _FakeLauncher(error=RuntimeError(_MARKUP))
    app = RemoteAgentsTui(_context(sessions=launcher))

    # `notifications=True` because `run_test` disables them by default, which leaves the
    # screen with no `ToastRack` and nothing to mount into. `App._notifications` is populated
    # either way — that is what every other test here reads — so this is the one case that
    # needs the real widget, and the one that would otherwise assert over an empty query.
    async with app.run_test(size=(100, 40), notifications=True) as pilot:
        await pilot.pause()
        await _walk_to_review(app, pilot)
        await app.screen.choose("launch")
        # Two pauses: `notify` posts a message, and the handler schedules the rack's own
        # `show` with `call_later`, so the toast is not mounted until a second pass.
        await pilot.pause()
        await pilot.pause()
        toasts = [toast.render().plain for toast in app.screen.query(_Toast)]

    assert toasts, "no toast was mounted, so this asserts nothing about how one renders"
    assert any(_MARKUP in rendered for rendered in toasts), (
        f"the toast consumed {_MARKUP!r} as markup; toasts were {toasts!r}"
    )


# The breadcrumb -----------------------------------------------------------------


async def test_the_trail_grows_as_the_owner_walks_into_a_flow() -> None:
    """The header answers "where am I", which is what the status line stopped trying to.

    Asserted as a sequence rather than at one position: a breadcrumb that is correct only at
    the end is a label, and the whole reason it is built from the screen stack is that every
    position on the way has to be right too.
    """
    app = RemoteAgentsTui(_context())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        trail = [breadcrumb(app)]
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        trail.append(breadcrumb(app))
        await app.screen.choose("claude")
        await pilot.pause()
        trail.append(breadcrumb(app))

    # Three positions, and the last of them is the review. It used to be four, and the
    # fourth read `… › claude › Review` because the label step sat between the agent and the
    # commit point and carried the agent into the trail itself.
    #
    # With that step gone the review names the agent, which is the same convention applied one
    # position earlier: a crumb names the choice that led here. So the *word* "Review" leaves
    # the trail and the *agent* stays in it — which is the half that matters, since a commit
    # point that does not say which agent it is about to launch is the defect the label step's
    # own docstring recorded being fixed for.
    assert trail == [
        "Projects",
        "Projects › infra/existing",
        "Projects › infra/existing › claude",
    ]


async def test_the_longest_trail_still_fits_the_header_at_the_committed_width() -> None:
    """A trail long enough to be elided is a trail that stops naming what it was drawn for.

    The resume flow is the deepest in this surface, and it is the one where the elision would
    cost most: its conversation descriptions are echoed from an agent's own output and so are
    the longest values any position here carries, which is why they go to the status line
    rather than into the trail. What the header is relied on for is the agent, and this is what
    pins that it survives.

    It was a crumb deeper still, at a confirmation that stood after the conversation list; that
    step is gone, so the trail this measures is one shorter than when the measurement was
    taken. Kept rather than re-tuned: a bound that still holds with a shorter trail is a bound
    that holds, and loosening it to match would only make the check weaker.

    Measured against `HeaderTitle`'s own rendered width at 100 columns — the width the
    snapshot baselines are committed at — rather than against a guessed character budget, so
    it fails if the title's layout changes and not only if the wording grows.
    """
    conversations = _Conversations()
    app = RemoteAgentsTui(_context(conversations=conversations))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        # Stop here: this is the deepest position the flow has now, and choosing a row on it
        # issues the resume rather than walking one step further.
        trail = breadcrumb(app)
        drawn = app.screen.query_one(_HeaderTitle).render_line(0).text

    assert trail == "Projects › Resume › infra/existing › claude"
    assert "…" not in drawn, f"the header elided the trail at 100 columns: {drawn!r}"
    assert "claude" in drawn, (
        "the agent fell out of the header, and the conversation list names it nowhere else"
    )


async def test_leaving_a_flow_shortens_the_trail_again() -> None:
    """The trail is the stack, so a pop has to shorten it without anybody maintaining a path."""
    app = RemoteAgentsTui(_context())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        deep = breadcrumb(app)
        await app.action_back()
        await pilot.pause()
        back = breadcrumb(app)

    assert deep == "Projects › infra/existing › claude"
    assert back == "Projects › infra/existing"


async def test_a_failure_status_carries_its_severity_as_a_design_system_class() -> None:
    """`$error` from the theme, not a colour literal, so it resolves in light and dark alike."""
    from textual.widgets import Static

    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        screen = app.screen
        screen.set_status("The development root could not be read.", severity="error")
        await pilot.pause()
        region = screen.query_one("#status", Static)
        errored = region.has_class("-error")

        screen.set_status("Choose a project.", severity="information")
        await pilot.pause()
        cleared = region.has_class("-error")

    assert errored, "a failure status was not marked as one"
    assert not cleared, "the severity outlived the message it belonged to"


async def test_severity_is_never_the_only_signal() -> None:
    """The judgment a colour cannot carry: under NO_COLOR the words are all that is left.

    Every call site in this package that passes a non-default severity is checked to be
    saying what went wrong in words too — asserted over the source rather than over one
    example, because the rule is about the set of call sites and a new one is exactly what
    would break it.

    **What this cannot check, stated rather than implied.** For a literal message it verifies
    there are words. For a computed one — `failure.status`, an f-string — it verifies only
    that a message is passed at all, because whether that value reads as an explanation is
    not a property of the syntax tree. It catches `set_status("", severity="error")` and
    `set_status(severity="error")`; it cannot catch a variable holding an empty string. The
    judgment half belongs to the gate's reader, and this narrows what they have to read.
    """
    import ast

    # `_SURFACE_ROOT`, not a relative path. The first version of this line read
    # `Path("src/remote_agents/adapters/tui")`, which resolves against the *cwd*: run from
    # anywhere but the repo root the glob yields nothing, `offenders` is empty, and the check
    # reports green having read no files at all. This file already had an absolute root and a
    # test guarding the older sweep against exactly that — the new check was the one left out.
    offenders: list[str] = []
    swept = 0
    for source in sorted(_SURFACE_ROOT.rglob("*.py")):
        swept += 1
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            attribute = getattr(node.func, "attr", None)
            if attribute != "set_status":
                continue
            severities = [kw for kw in node.keywords if kw.arg == "severity"]
            if not severities:
                continue
            value = severities[0].value
            if isinstance(value, ast.Constant) and value.value == "information":
                continue
            message = node.args[0] if node.args else None
            if not isinstance(message, ast.Constant | ast.JoinedStr | ast.Name | ast.Attribute):
                offenders.append(f"{source}:{node.lineno}")
                continue
            if isinstance(message, ast.Constant) and not str(message.value).strip():
                offenders.append(f"{source}:{node.lineno}")

    assert swept >= 5, (
        f"the sweep read {swept} files under {_SURFACE_ROOT}; it found nothing to check"
    )
    assert offenders == [], f"a severity-coloured status with no words: {offenders}"


async def test_the_severity_colour_comes_from_the_theme_and_changes_with_it() -> None:
    """Nothing in the committed visual net would notice if the colour block were deleted.

    A gate evaluator pointed this out and it is exactly right: `$foreground` on `#status` is
    already the default, `$surface` on the output pane equals the screen's own background,
    `$text-muted` reaches only a disabled row no baseline renders, and `$error`/`$warning`
    reach only a severity no baseline sets. So the committed SVGs — this repo's only assertion
    about what the owner *sees* — are silent about the whole change.

    This is that assertion. It resolves the rendered colour under two themes and requires
    them to differ, which is the property a design-system token has and a hex literal does
    not: a literal would render identically under both and fail here.
    """
    from textual.widgets import Static

    app = RemoteAgentsTui(_context())
    seen: dict[str, tuple[int, int, int]] = {}

    async with app.run_test() as pilot:
        screen = app.screen
        region = screen.query_one("#status", Static)
        # `textual-dark` and `textual-light` were the obvious pair and are the wrong one:
        # both define `error` as the same `#ba3c5b`, so a hex literal would have passed. These
        # two genuinely differ (`#ba3c5b` against gruvbox's `#fb4934`), which is what makes
        # the assertion below able to fail.
        for theme in ("textual-dark", "gruvbox"):
            app.theme = theme
            screen.set_status("The managed sessions could not be read.", severity="error")
            await pilot.pause()
            colour = region.styles.color
            seen[theme] = (colour.r, colour.g, colour.b)

        # And the neutral case must not be wearing the error colour.
        app.theme = "textual-dark"
        screen.set_status("Choose a project.")
        await pilot.pause()
        neutral = region.styles.color
        neutral_rgb = (neutral.r, neutral.g, neutral.b)

    assert seen["textual-dark"] != seen["gruvbox"], (
        f"the error colour is theme-independent, so it is a literal, not a token: {seen}"
    )
    assert neutral_rgb != seen["textual-dark"], "a neutral status renders in the error colour"


async def test_the_review_status_names_the_terminal_handover_it_is_about_to_do() -> None:
    """The surface's most irreversible act, said on the screen that commits to it.

    A ready launch **execs away** (DEC-023): `attach_to` replaces this process with the tmux
    client, so detaching later returns the owner to their shell rather than to this app. That is
    a bigger consequence than anything else the surface does, and until now it was documented in
    `adapters/tui/attach.py`'s docstring and in the README and stated nowhere the owner could
    see it.

    The line it replaces read `Label: none. Launch, or go back.` — which, once the label step
    was removed, rendered the absence of a step that no longer existed. Its own comment said the
    label was the one part of the selection the breadcrumb could not carry; with the label gone
    the trail carries the whole selection, and this region is free to say what the act does.

    **Informational, not a severity** (DEC-010): this is an instruction about what is about to
    happen, not a report of a condition, and that decision is explicit that a severity is
    carried only when the words carry it. The neutral-colour half is asserted below for that
    reason — a warning-coloured instruction is the exact defect DEC-010 was written against.
    """
    from textual.widgets import Static

    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await _walk_to_review(app, pilot)
        assert position(app) == "REVIEW", f"the walk landed on {position(app)}"
        said = _status(app)
        region = app.screen.query_one("#status", Static)
        review_rgb = (region.styles.color.r, region.styles.color.g, region.styles.color.b)

        # The same region, on the same screen, carrying a real condition. Read second so the
        # comparison is between two renders of one widget rather than between two screens.
        app.screen.set_status("The managed sessions could not be read.", severity="error")
        await pilot.pause()
        error = region.styles.color
        error_rgb = (error.r, error.g, error.b)

    lowered = said.casefold()
    assert "terminal" in lowered and "pane" in lowered, (
        f"the review must say what going through with it does to this terminal; it said {said!r}"
    )
    assert "label" not in lowered, (
        f"the review still mentions the label step that no longer exists: {said!r}"
    )
    assert review_rgb != error_rgb, (
        "an instruction is rendering in the severity colour, which is what DEC-010 forbids"
    )


async def test_a_failed_read_still_says_what_failed_after_its_toast_has_gone() -> None:
    """The toast expires; the status region is what is left, and it used to report nothing.

    A gate evaluator drove this: with `_FAILURE_TIMEOUT` at 20 seconds, an unreadable store
    left `Press escape to return to the project list.` on screen — a sentence naming no
    condition — and after the toast had gone it was distinguishable from an ordinary empty
    list only by the *absence* of the empty-state row. The surface's own better paths already
    did this correctly: a launch that produced nothing leaves "Nothing was started." up.

    Asserted on the status text rather than on the toast, deliberately: the toast is the half
    with a lifetime, so a test that read it would pass on exactly the arrangement that failed.
    """
    from textual.widgets import Static

    class _Unreadable:
        async def refresh_readiness(self):
            raise RuntimeError("database is locked")

        async def list_sessions(self):
            raise RuntimeError("database is locked")

        async def copy_attach(self, _session_id):
            return None

    app = RemoteAgentsTui(_context(sessions=_Unreadable()))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await pilot.pause()
        region = app.screen.query_one("#status", Static)
        said = str(region.content)
        marked = region.has_class("-error")

    assert "could not be read" in said, f"the status named no condition: {said!r}"
    assert marked, "a status that reports a failure is not marked as one"
