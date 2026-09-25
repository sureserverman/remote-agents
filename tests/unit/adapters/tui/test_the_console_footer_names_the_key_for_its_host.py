"""What the key row *calls* an entry depends on where the surface is hosted — BL-097, DEC-096.

**Since DEC-105 the console's key row is the tmux bar, not a Textual Footer** (R9, signed off
2026-09-25): under console hosting no screen composes a Footer, and the bar is built from
`status_bar_keys()`. So the console side of each claim below reads the bar's table, and the
bare side still reads the rendered Footer.

`F10` is `quit`. In a bare terminal that means "leave the app" and the footer is right to say
so. In a **console surface pane** it closes the whole console: four panes go, the shell comes
back, and every agent session keeps running.

**This file used to assert the opposite of its own subject, and the history is the argument.**
`quit` in a console pane once meant "destroy the pane the owner is reading" — those panes carry
no `remain-on-exit`, so the process ending closed the pane and tmux reflowed the layout over the
gap. The owner pressed it on 2026-09-17, lost the sessions pane, and ran degraded for twenty
minutes. The fix shipped then was **de-advertisement**: the key stayed bound and the console's
footer stopped offering it. That was the cheap honest half.

DEC-096 is the other half, and it makes the entry true again, so it is drawn again. What it may
not do is keep the bare terminal's word: off a console the press costs one process the owner
started on purpose, and here it costs four panes. So the console's entry reads `close console`.

Every claim below is made **over `FUNCTION_KEYS` as a whole** rather than over `f10`. A twelfth
key added to the table is visited by the same loops on the commit that adds it, which is the
only way this stays a rule instead of becoming a fact about one key.

The hosting seam is `TuiContext.console_hosted`, wired by the composition root exactly as the
other console-only fields are (DEC-046). These tests therefore state hosting the way production
states it -- a composed field -- rather than by setting `$TMUX`, which is deliberately not what
the app reads.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from backends import SessionUseCaseDouble, backend_for
from rich.console import Console as RichConsole
from textual.widgets._footer import FooterKey
from textual.widgets._key_panel import BindingsTable

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.keys import (
    CONSOLE_FOOTER_LABELS,
    FUNCTION_KEYS,
    status_bar_keys,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Launcher(SessionUseCaseDouble):
    async def refresh_readiness(self):
        return (_record(),)

    async def list_sessions(self):
        return (_record(),)

    async def copy_attach(self, _session_id):
        return None


class _Creator:
    def available_areas(self):
        return ("dev-area", "infra")


def _context(*, console_hosted: bool) -> TuiContext:
    return replace(
        TuiContext(
            backend=backend_for(
                sessions=_Launcher(),  # type: ignore[arg-type]
                projects=_Creator(),  # type: ignore[arg-type]
                refresh_catalogue=lambda: (_PROJECT,),
                catalogue=(_PROJECT,),
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        ),
        console_hosted=console_hosted,
    )


@dataclass(frozen=True, slots=True)
class _Reading:
    """What one hosting mode's resting position actually shows."""

    #: The keys the `Footer` widget *composed a `FooterKey` for* -- the rendered footer, read
    #: off the widgets Textual built, not off our own filter over `active_bindings`. A test
    #: that re-applied `if binding.show` would pass against a footer that had stopped honouring
    #: it, which is the one thing this change turns on.
    drawn: frozenset[str]
    #: The *words* those footer entries carry, which is what the owner actually reads. Held
    #: apart from `drawn` so one assertion can be made in the owner's vocabulary rather than in
    #: the table's: `quit` is what the console must stop offering, whichever key carries it.
    drawn_labels: frozenset[str]
    #: Every key still bound at this position. `Footer` draws a subset of this; a key missing
    #: from it is unbound or refused by `check_action`, either of which would be a removal.
    bound: frozenset[str]
    #: F1's help panel, rendered to plain text. `BindingsTable` renders `active_bindings`
    #: without filtering on `show`, which is exactly what makes de-advertisement honest.
    panel: str
    #: How `App.get_key_display` spelled each bound key, so the panel assertion can look for
    #: the string the owner actually reads rather than guessing at its case.
    displays: dict[str, str]


async def _reading(*, console_hosted: bool) -> _Reading:
    """Drive the resting position under one hosting mode and read all three surfaces."""
    app = RemoteAgentsTui(_context(console_hosted=console_hosted))
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        keys = list(app.screen.query(FooterKey))
        drawn = frozenset(widget.key for widget in keys)
        drawn_labels = frozenset(widget.description for widget in keys)
        active = dict(app.screen.active_bindings)
        displays = {key: app.get_key_display(entry.binding) for key, entry in active.items()}
        # F1's own act, reached through the action rather than the keystroke: what is under
        # test is what the panel lists, not whether F1 arrives.
        app.action_help()
        await pilot.pause()
        table = app.screen.query_one(BindingsTable).render_bindings_table()
        recorder = RichConsole(width=200, no_color=True)
        with recorder.capture() as capture:
            recorder.print(table)
        panel = capture.get()
    return _Reading(drawn, drawn_labels, frozenset(active), panel, displays)


def test_the_console_relabels_exactly_the_footer_entries_whose_meaning_its_host_changes() -> None:
    """*Why* an entry is relabelled, as a predicate over the table rather than as a list.

    An entry is relabelled because the act it names costs something different here: `quit` off
    a console ends one process, and in a console pane it closes the console. So the relabelled
    set is not an arbitrary one — it is the footer entries whose action quits. Asserting the
    equality both ways stops it drifting in either direction: a twelfth quitting key with no
    console wording fails here, and so does a wording nobody wrote a reason for.
    """
    quits = {entry.key for entry in FUNCTION_KEYS if entry.action == "quit"}

    assert set(CONSOLE_FOOTER_LABELS) == quits, (
        "the console renames a footer entry because the act it names costs something different "
        f"there, so the relabelled set must be exactly the table's quitting keys: {quits}"
    )
    assert all(entry.footer for entry in FUNCTION_KEYS if entry.key in CONSOLE_FOOTER_LABELS), (
        "relabelling a key the footer never draws says nothing; the set must be footer keys"
    )
    assert all(
        label != entry.label
        for entry in FUNCTION_KEYS
        for label in [CONSOLE_FOOTER_LABELS.get(entry.key)]
        if label is not None
    ), "a console wording identical to the table's own is a relabel that relabels nothing"


async def test_both_hostings_draw_every_footer_key_the_table_declares() -> None:
    """Neither hosting withholds a key the table offers, and that is the assertion worth keeping.

    Off a console the Footer draws the table's footer entries. Under console hosting the
    Footer is gone (R9) and the tmux bar draws every key in the table, so a key dropped from
    the console's row everywhere -- a removal wearing this change's clothes -- cannot pass.
    """
    bare = await _reading(console_hosted=False)
    console = await _reading(console_hosted=True)
    on_the_bar = {f"f{key.number}" for key in status_bar_keys()}

    assert console.drawn == frozenset(), (
        f"under console hosting the tmux bar is the key row; a Footer drew {sorted(console.drawn)}"
    )
    for entry in FUNCTION_KEYS:
        assert (entry.key in bare.drawn) is entry.footer, (
            f"off a console the footer draws what the table says: {entry.key} has "
            f"footer={entry.footer} and is {'drawn' if entry.key in bare.drawn else 'absent'}"
        )
        assert entry.key in on_the_bar, f"the console's bar does not draw {entry.key}"


async def test_the_console_footer_says_close_console_where_a_bare_terminal_says_quit() -> None:
    """The claim in the owner's own vocabulary, which is the only one they actually read.

    Deliberately not derived from `CONSOLE_FOOTER_LABELS` on the bare side: `quit` is the word
    a terminal-owning app must offer, and `close console` is the word it must not. Stating both
    literally is what catches a mapping that is correct and applied to the wrong hosting. The
    console's words are the bar's, since the bar is its only key row (DEC-105).
    """
    bare = await _reading(console_hosted=False)
    bar_labels = {key.label for key in status_bar_keys()}

    assert "quit" in bare.drawn_labels, "off a console the footer still offers quit"
    assert "close console" not in bare.drawn_labels, (
        f"a bare terminal has no console to close: {sorted(bare.drawn_labels)}"
    )

    assert "close console" in bar_labels, (
        f"the console's bar does not say what F10 now does: {sorted(bar_labels)}"
    )
    assert "quit" not in bar_labels, (
        "the console's bar still reads `quit`, which understates a press that closes four "
        f"panes: {sorted(bar_labels)}"
    )


async def test_hosting_changes_the_words_and_never_whether_a_key_is_bound() -> None:
    """The half that keeps this a relabel rather than a removal (DEC-093, DEC-095).

    **Hosting decides wording and nothing else**: whether a key is in `active_bindings` -- the
    map `App.run_action` dispatches through, so a key absent from it does not work -- must be
    the same answer in both modes for every row. That is the assertion a removal disguised as
    this change would fail, and it covers a twelfth key on the commit that adds it.

    And the relabelled keys specifically are still listed in F1's panel in both hostings, which
    renders `active_bindings` without filtering -- the property the key was borrowed from htop
    for.

    Not every row is in `active_bindings` here, and that is correct rather than a gap: the
    session-shaped keys are refused by `check_action` at the resting position, because the
    dashboard owns a sessions cursor and binds none of the bare row letters. So the assertion
    is that hosting does not *change* that answer, not that the answer is always yes.
    """
    bare = await _reading(console_hosted=False)
    console = await _reading(console_hosted=True)

    for entry in FUNCTION_KEYS:
        assert (entry.key in bare.bound) is (entry.key in console.bound), (
            f"console hosting changed whether {entry.key} is bound at all; it may change only "
            "what the footer says (DEC-093, DEC-095)"
        )
        if entry.key not in CONSOLE_FOOTER_LABELS:
            continue
        for where, reading in (("off a console", bare), ("under console hosting", console)):
            assert entry.key in reading.bound, (
                f"{entry.key} is not bound {where}; relabelling a footer entry must never "
                "unbind the key (DEC-093)"
            )
            assert reading.displays[entry.key] in reading.panel, (
                f"F1's panel does not list {entry.key} {where}, so the key would be bound, "
                "drawn nowhere, and reachable only by knowing it is there"
            )
