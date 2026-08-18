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

**One of those three sinks lost the source this file used to drive it through, and an earlier
version of this paragraph drew the wrong conclusion from that.** `#status` received the
conversation description the moment a conversation was selected, on the confirmation step that
stood between the list and the resume; that step was removed when the local surface stopped
confirming a resume the bot does not confirm. The paragraph then claimed the sink had lost its
*last* un-authored source and that "every interpolated exception and provider reason [is] routed
to a toast instead" — a repo-wide invariant that is one grep away from false. A Tier-2 review
found it, and the sweep behind it found more than the review did:

    grep -rn 'set_status(f"' src/remote_agents/adapters/tui/

`app.py`'s `answer_trust` writes an exception's own text there, the inspect screen writes the
owner's typed search query, and the project review writes the owner's typed project name. The
sink is very much still live for text this app did not author.

So the driven test is **retargeted rather than deleted**: it now reaches `#status` through the
exception path, which is un-authored in the strongest sense — a string from another layer
entirely. The resume-specific route it used to take is gone, but the property it protects is
not, and deleting it would have left the sink covered only by `markup=False` and the structural
sweep below while the docstring claimed there was nothing left to cover.
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
    #: When set, `answer_trust` raises with this text. The one live path that still writes a
    #: string this app did not author into `#status`, and so the one that can drive the sink.
    trust_error: str | None = None

    async def refresh_readiness(self):
        return (self.record,)

    async def trust_state(self, _session_id):
        from remote_agents.domain.trust import TrustState

        return TrustState.AWAITING

    async def answer_trust(self, _command):
        if self.trust_error is not None:
            raise RuntimeError(self.trust_error)
        from remote_agents.domain.trust import TrustState

        return TrustState.UNKNOWN

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
    *,
    project: CatalogProject,
    record: SessionRecord | None = None,
    description: str = "plain",
    trust_error: str | None = None,
) -> TuiContext:
    return TuiContext(
        launcher=_Launcher(record=record or _record(), trust_error=trust_error),  # type: ignore[arg-type]
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


async def test_the_status_line_shows_a_markup_bearing_failure_literally() -> None:
    """The rows were only one sink. `#status` receives text this app did not author too.

    **Retargeted, not deleted.** This used to drive the conversation description into `#status`
    by selecting a conversation — a path that ran through a confirmation step which no longer
    exists. The sink does still take un-authored text, so the test follows it to a route that
    survives: `RemoteAgentsTui.answer_trust` writes an exception's own `str()` straight to
    `set_status`, which is un-authored in the strongest sense available here — the string comes
    from another layer entirely and can contain anything.

    An unbalanced `[` there raises `MarkupError` in a widget that parses markup, which is the
    same defect this file closed at the rows and would have reopened at a sink nobody was
    driving any more.
    """
    project = CatalogProject("opaque-existing", "existing", "infra", "Registered")
    app = RemoteAgentsTui(_context(project=project, trust_error=_MARKUP_DESCRIPTION))
    async with app.run_test(size=(200, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await pilot.pause()
        await app.show_detail(str(_record().session_id))
        await pilot.pause()
        await app.screen.answer_trust()
        await pilot.pause()
        screen = _rendered(app)

    assert _MARKUP_DESCRIPTION in screen, (
        f"the status line consumed {_MARKUP_DESCRIPTION!r} as markup; screen was {screen!r}"
    )


async def test_the_header_shows_a_markup_bearing_session_label_literally() -> None:
    """The breadcrumb is the third sink for the owner's own label, and it renders differently.

    `record.display.rendered` used to reach only `#status` and the rows, both of which are
    explicitly `markup=False`. The status split moved the session's name into the header,
    where nothing in this codebase set that flag — `Header` renders through
    `App.format_title`, which builds a `Content` from the plain string rather than parsing it
    as markup. That is the property under test, and it is a property of Textual rather than of
    a flag this app can see, so it is worth an assertion rather than an assumption.

    Rendered at 200 columns because `HeaderTitle` is `text-overflow: ellipsis`: at 100 the
    trail is elided mid-label and the test would fail on the width rather than on the markup.
    """
    project = CatalogProject("opaque-existing", "existing", "infra", "Registered")
    app = RemoteAgentsTui(_context(project=project, record=_record(_MARKUP_LABEL)))
    async with app.run_test(size=(200, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        screen = _rendered(app)
        assert _MARKUP_LABEL in screen, (
            f"the header consumed {_MARKUP_LABEL!r} as markup; screen was {screen!r}"
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
