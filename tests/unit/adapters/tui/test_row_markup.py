"""Row text is shown to the owner, never interpreted as console markup.

`_fill` wrapped every row in a `Label`, whose content Textual parses as console markup. Three
different externally-influenced strings reach it, and all three were being mangled:

* the project list's registered/unregistered tag, built as `f"...  [{project.group}]"` —
  `[Registered]` parsed as a style tag and vanished, so README.md's claim that each row names
  its group was false on screen and the owner could not tell a registered project from a
  discovered one;
* the **owner's own session label**, free text bounded only by `max_label_length`, so a label
  like `[bold]urgent[/bold]` silently lost its brackets and restyled the row;
* the **conversation description**, which `domain/conversations.py` sources from the agent's
  own last prompt or generated title. That one matters most: it is text this app echoes from
  another program, and `[link=…]` is a live hyperlink directive, so an agent's output could
  put a clickable link into the owner's terminal.

Those three sources reach **three separate sinks**, and that is the part worth remembering.
Fixing the row `Label` looked like a class fix and was not: review then found `#status` — which
receives the description the moment a conversation is selected, and the custom label via
`record.display.rendered` — and `#output`, which renders the session's raw captured pane
output, filtered by `sanitize_terminal_text` for control sequences and NUL but never for
brackets. Both still crashed. So the same defect was closed one sink at a time, twice, and
missed one both times.

Hence the last test here: a sweep asserting no markup-rendering construction in the surface
package is missing `markup=False`. It is the only part of this file that can fail for a sink
nobody has thought of yet, which is what the previous two rounds needed and did not have.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from remote_agents.adapters.tui.app import RemoteAgentsTui
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

_SESSION_ID = SessionId.new()
_REFERENCE = ConversationReference("c-" + "0" * 14 + "01")
# Each is a string a real source can produce that console markup would eat or act on.
_MARKUP_LABEL = "[bold]urgent[/bold]"
_MARKUP_DESCRIPTION = "check [link=file:///etc/passwd]this[/link]"


def _rendered(app: RemoteAgentsTui) -> str:
    """What is actually on screen, not what was handed to the widget.

    A row hands back the string as supplied, markup and all — it was `Label.content` then and
    it is `Option.prompt` now — so asserting against it passes while the screen shows
    something else entirely, which is exactly how this defect survived. The screenshot is the
    same artifact the snapshot baselines compare, so this reads the rendered result rather
    than the input to it.
    """
    svg = app.export_screenshot(title="rows")
    text = html.unescape("".join(re.findall(r"<text[^>]*>(.*?)</text>", svg, re.S)))
    # The SVG breaks a line into styled runs and pads with non-breaking spaces; collapse both
    # so an assertion can be written about the words rather than about the layout.
    return re.sub(r"\s+", " ", text.replace("\xa0", " "))


def _record(custom_label: str | None = None) -> SessionRecord:
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1, custom_label),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Launcher:
    record: SessionRecord = field(default_factory=_record)

    async def refresh_readiness(self):
        return (self.record,)

    async def list_sessions(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return None


@dataclass(slots=True)
class _Conversations:
    description: str = "plain"

    async def catalogue(self, query):
        return ConversationCataloguePage((self._summary(),), query.page, 1)

    async def resolve_for_resume(self, _reference):
        return ResolvedConversation(self._summary(), ProviderConversationId("abc"))

    async def capabilities(self):
        return (
            ProfileResumeCapability(
                ProfileId("claude"), catalogue_available=True, selected_resume_available=True
            ),
        )

    def _summary(self) -> ConversationSummary:
        return ConversationSummary(
            _REFERENCE,
            ProfileId("claude"),
            ProjectId("opaque-existing"),
            ConversationState.RESUMABLE,
            datetime.now(UTC),
            description=self.description,
        )


def _context(
    *, project: CatalogProject, record: SessionRecord | None = None, description: str = "plain"
) -> TuiContext:
    return TuiContext(
        launcher=_Launcher(record=record or _record()),  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (project,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(project,),
        conversations=_Conversations(description=description),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("group", ["Registered", "Unregistered"])
async def test_the_project_row_names_its_group(group: str) -> None:
    """README.md states that each project row names its group; this is that claim."""
    project = CatalogProject("opaque-existing", "existing", "infra", group)
    app = RemoteAgentsTui(_context(project=project))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = _rendered(app)
        assert group in screen, f"no project row names its {group!r} group; screen was {screen!r}"


async def test_a_session_label_containing_markup_is_shown_literally() -> None:
    """The label is the owner's own free text and must survive to the screen intact."""
    project = CatalogProject("opaque-existing", "existing", "infra", "Registered")
    app = RemoteAgentsTui(_context(project=project, record=_record(_MARKUP_LABEL)))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await pilot.pause()
        screen = _rendered(app)
        assert _MARKUP_LABEL in screen, (
            f"the label {_MARKUP_LABEL!r} was consumed as markup; screen was {screen!r}"
        )


async def test_a_conversation_description_containing_markup_is_shown_literally() -> None:
    """Text echoed from the agent must not be able to style or link the owner's terminal."""
    project = CatalogProject("opaque-existing", "existing", "infra", "Registered")
    app = RemoteAgentsTui(_context(project=project, description=_MARKUP_DESCRIPTION))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.action_resume()
        await app.screen.choose("opaque-existing")
        await app.screen.choose("claude")
        await pilot.pause()
        screen = _rendered(app)
        assert _MARKUP_DESCRIPTION in screen, (
            f"the description {_MARKUP_DESCRIPTION!r} was consumed as markup; screen was {screen!r}"
        )


async def test_the_status_line_shows_a_markup_bearing_description_literally() -> None:
    """The rows were only one sink. The status line receives the same description.

    Fixing `_fill` alone left this open: selecting a conversation writes its description
    straight into `#status`, before any confirmation, and an unbalanced bracket raised
    `MarkupError` there exactly as it did in the list.
    """
    project = CatalogProject("opaque-existing", "existing", "infra", "Registered")
    app = RemoteAgentsTui(_context(project=project, description=_MARKUP_DESCRIPTION))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.action_resume()
        await app.screen.choose("opaque-existing")
        await app.screen.choose("claude")
        await app.screen.choose(str(_REFERENCE))
        await pilot.pause()
        screen = _rendered(app)
        assert _MARKUP_DESCRIPTION in screen, (
            f"the status line consumed {_MARKUP_DESCRIPTION!r} as markup; screen was {screen!r}"
        )


async def test_the_status_line_shows_a_markup_bearing_session_label_literally() -> None:
    """`record.display.rendered` interpolates the owner's label into a dozen status strings."""
    project = CatalogProject("opaque-existing", "existing", "infra", "Registered")
    app = RemoteAgentsTui(_context(project=project, record=_record(_MARKUP_LABEL)))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        screen = _rendered(app)
        assert _MARKUP_LABEL in screen, (
            f"the detail status consumed {_MARKUP_LABEL!r} as markup; screen was {screen!r}"
        )


async def test_captured_output_bearing_markup_is_shown_literally() -> None:
    """The inspect pane renders a session's raw captured output — the least trusted string.

    `sanitize_terminal_text` filters control sequences and NUL, never brackets, so before
    this fix an agent that printed an unbalanced `[` took down the screen displaying it.
    """
    project = CatalogProject("opaque-existing", "existing", "infra", "Registered")
    app = RemoteAgentsTui(_context(project=project))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.show_output(f"agent said: {_MARKUP_DESCRIPTION}")
        await pilot.pause()
        screen = _rendered(app)
        assert _MARKUP_DESCRIPTION in screen, (
            f"the output pane consumed {_MARKUP_DESCRIPTION!r} as markup; screen was {screen!r}"
        )


def test_no_markup_parsing_widget_is_constructed_without_the_flag() -> None:
    """The sweep, because this class was closed one sink at a time and twice missed one.

    `_fill`'s `Label` was fixed first and looked like a class fix; review then found the
    `#status` and `#output` Statics reachable by the same untrusted strings, each still
    crashing. Naming the set here means a fourth sink fails this rather than being found by
    whoever hits the crash.
    """
    import ast
    from pathlib import Path

    surface = Path(__file__).resolve().parents[4] / "src" / "remote_agents" / "adapters" / "tui"
    # `notify` is swept alongside the widgets deliberately, not speculatively: `App.notify`
    # defaults to `markup=True` exactly as they do, it is not a widget *construction* so a
    # walk keyed on class names alone cannot see it, and the planned status work introduces
    # precisely the call this catches — `self.notify(f"...{record.display.rendered}...")`.
    #
    # `OptionList` is in the set because it is the *row* sink, and it is the one whose flag
    # is easiest to leave off: `Option` has no `markup` argument, so there is nothing on a
    # row to forget — the widget renders each option with
    # `visualize(self, option.prompt, markup=self._markup)`, and `OptionList.__init__`
    # defaults that to `True`. An `OptionList(id="choices")` with no flag therefore reopens
    # exactly the defect this module's docstring describes, silently, on the sink that
    # receives all three untrusted strings. Adding it here is what makes this sweep cover the
    # rows again after they stopped being `Label`s.
    #
    # `Label` stays although `_fill` no longer builds one: nothing stops the next screen from
    # reaching for it, and a name in this set costs nothing until it is used.
    #
    # Stated limits, so the guarantee is not overstated: this catches direct, unaliased
    # calls. An aliased import (`Static as St`), an attribute construction
    # (`widgets.Static(...)`), a factory, or a subclass would evade it; none of those shapes
    # exist here. `markup` is keyword-only on all four, so inspecting only keywords is
    # correct rather than a gap.
    parses_markup = {"Static", "Label", "OptionList"}
    unguarded: list[str] = []
    for module in sorted(surface.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in parses_markup:
                called = node.func.id
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "notify":
                called = "notify"
            else:
                continue
            flag = next((kw for kw in node.keywords if kw.arg == "markup"), None)
            if flag is None or not (
                isinstance(flag.value, ast.Constant) and flag.value.value is False
            ):
                unguarded.append(f"{module.name}:{node.lineno} {called}(...)")
    assert not unguarded, (
        "these render their content as console markup and were used without "
        f"markup=False: {unguarded}. Row, status, output and notification text is displayed, never "
        "interpreted -- see this module's docstring."
    )
