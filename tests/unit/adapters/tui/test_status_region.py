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
from textual.widgets import OptionList, Static

# Neither is re-exported from `textual.widgets`. Both are system widgets this app never
# constructs itself — the toast is raised by `notify`, the header title is composed inside
# `Header` — so reaching past the package boundary is the only way to assert on either.
from textual.widgets._header import HeaderTitle as _HeaderTitle
from textual.widgets._toast import Toast as _Toast
from tui_feedback import announcements, breadcrumb
from tui_feedback import status as _status

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import ALL_SCREENS
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.commands import LaunchCommand
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
        "launcher": _FakeLauncher(),
        "creator": _FakeCreator(),
        "profiles": (ProfileChoice("claude", True),),
        "refresh_catalogue": lambda: (_EXISTING,),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_EXISTING,),
    }
    arguments.update(overrides)
    return TuiContext(**arguments)  # type: ignore[arg-type]


async def _walk_to_review(app: RemoteAgentsTui, pilot) -> None:
    await app.screen.choose("opaque-existing")
    await pilot.pause()
    await app.screen.choose("claude")
    await pilot.pause()
    app.screen.submit("")
    await pilot.pause()


# The layout ---------------------------------------------------------------------


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
    assert height == 1, f"the status region is {height} lines high, not 1"


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
    app = RemoteAgentsTui(_context(launcher=launcher))

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
    app = RemoteAgentsTui(_context(launcher=launcher))

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
        trail.append(breadcrumb(app))
        await app.screen.choose("claude")
        await pilot.pause()
        trail.append(breadcrumb(app))
        app.screen.submit("")
        await pilot.pause()
        trail.append(breadcrumb(app))

    assert trail == [
        "Projects",
        "Projects › infra/existing",
        "Projects › infra/existing › claude",
        "Projects › infra/existing › claude › Review",
    ]


async def test_the_longest_trail_still_fits_the_header_at_the_committed_width() -> None:
    """A trail long enough to be elided is a trail that stops naming what it was drawn for.

    The resume flow is the deepest in this surface — five crumbs at the confirmation — and it
    is the one where the elision would cost most, since `ResumeConfirmScreen` gives its
    *subject* to the status line specifically because the header cannot be trusted to show it
    whole. What the header is still relied on for there is the agent, and this is what pins
    that it survives.

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
        await app.screen.choose(str(_REFERENCE))
        await pilot.pause()
        trail = breadcrumb(app)
        drawn = app.screen.query_one(_HeaderTitle).render_line(0).text

    assert trail == "Projects › Resume › infra/existing › claude › Confirm"
    assert "…" not in drawn, f"the header elided the trail at 100 columns: {drawn!r}"
    assert "claude" in drawn, (
        "the agent fell out of the header, and the confirmation names it nowhere else"
    )


async def test_leaving_a_flow_shortens_the_trail_again() -> None:
    """The trail is the stack, so a pop has to shorten it without anybody maintaining a path."""
    app = RemoteAgentsTui(_context())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        deep = breadcrumb(app)
        await app.action_back()
        await pilot.pause()
        back = breadcrumb(app)

    assert deep == "Projects › infra/existing › claude"
    assert back == "Projects › infra/existing"
