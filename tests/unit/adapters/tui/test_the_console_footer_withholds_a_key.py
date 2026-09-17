"""What the footer advertises depends on where the surface is hosted -- BL-097.

`F10` is `quit`. In a bare terminal that means "leave the app" and the footer is right to say
so. In a **console surface pane** it means "destroy the pane the owner is reading": those panes
carry no `remain-on-exit`, so the pane closes outright and tmux reflows the layout over the
gap. The owner pressed it on 2026-09-17, lost the sessions pane, and ran degraded for twenty
minutes.

The fix chosen for that is **de-advertisement, not removal** (DEC-093, DEC-095): the key stays
bound, it still quits, and F1's panel still lists it -- the console's footer simply stops
offering it. So the three halves of that sentence are three assertions here, and every one of
them is made **over `FUNCTION_KEYS` as a whole** rather than over `f10`. A twelfth key added to
the table with `footer=True` is visited by the same loops on the commit that adds it, which is
the only way this rule stays a rule instead of becoming a fact about one key.

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
from remote_agents.adapters.tui.keys import CONSOLE_WITHHELD_FROM_FOOTER, FUNCTION_KEYS
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


def test_the_console_withholds_from_its_footer_exactly_the_keys_that_close_the_pane() -> None:
    """*Why* a key is withheld, stated as a predicate over the table rather than as a list.

    A console surface pane has no `remain-on-exit`: an action that ends this process ends the
    pane, and the layout reflows over the gap. So the set the console withholds is not an
    arbitrary one -- it is the footer entries whose action quits. Asserting the equality both
    ways is what stops the set from drifting in either direction: a twelfth key that quits and
    is not withheld fails here, and so does a key withheld for some reason nobody wrote down.
    """
    quits = {entry.key for entry in FUNCTION_KEYS if entry.action == "quit"}

    assert CONSOLE_WITHHELD_FROM_FOOTER == quits, (
        "the console withholds a footer entry because pressing it destroys the pane, so the "
        f"withheld set must be exactly the table's quitting keys: {quits}"
    )
    assert all(
        entry.footer for entry in FUNCTION_KEYS if entry.key in CONSOLE_WITHHELD_FROM_FOOTER
    ), "withholding a key the footer never drew anyway says nothing; the set must be footer keys"


async def test_the_console_footer_draws_every_footer_key_except_the_withheld_ones() -> None:
    """The de-advertisement itself, asserted over the whole table in both hosting modes.

    Both halves matter and only together: that the console drops the withheld entries, and
    that the standalone surface still draws them. A test asserting only the first passes just
    as well against a key dropped from the footer everywhere, which is a removal wearing this
    change's clothes.
    """
    bare = await _reading(console_hosted=False)
    console = await _reading(console_hosted=True)

    for entry in FUNCTION_KEYS:
        assert (entry.key in bare.drawn) is entry.footer, (
            f"off a console the footer draws what the table says: {entry.key} has "
            f"footer={entry.footer} and is {'drawn' if entry.key in bare.drawn else 'absent'}"
        )
        withheld = entry.key in CONSOLE_WITHHELD_FROM_FOOTER
        assert (entry.key in console.drawn) is (entry.footer and not withheld), (
            f"under console hosting {entry.key} should be "
            f"{'withheld from' if withheld else 'drawn in'} the footer"
        )

    # The same claim in the owner's own vocabulary, and deliberately not derived from the
    # withheld set: the word on the console's footer is what the owner reads, and `quit` is the
    # word that must not be there. A rename of the key or a second key acquiring the label is
    # caught here rather than passing because the key names still line up.
    assert "quit" in bare.drawn_labels, "off a console the footer still offers quit"
    assert "quit" not in console.drawn_labels, (
        f"the console footer still reads `quit`: {sorted(console.drawn_labels)}"
    )


async def test_a_key_withheld_from_the_console_footer_stays_bound_and_stays_in_f1() -> None:
    """The half that makes this a de-advertisement rather than a removal (DEC-093, DEC-095).

    Two properties, both read over the table. First, **hosting decides drawing and nothing
    else**: whether a key is in `active_bindings` -- the map `App.run_action` dispatches
    through, so a key absent from it does not work -- must be the same answer in both modes for
    every row. That is the assertion a removal disguised as this change would fail, and it
    covers a twelfth key on the commit that adds it.

    Second, the withheld keys specifically are still bound and still listed in F1's panel,
    which renders `active_bindings` without filtering on `show`. Without that second half the
    owner would have a key that works, is drawn nowhere, and is reachable only by knowing it is
    there -- which is the thing `FunctionKey.footer`'s own argument forbids.

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
            "what the footer draws (DEC-093, DEC-095)"
        )
        if entry.key not in CONSOLE_WITHHELD_FROM_FOOTER:
            continue
        for where, reading in (("off a console", bare), ("under console hosting", console)):
            assert entry.key in reading.bound, (
                f"{entry.key} is not bound {where}; withholding a footer entry must never "
                "unbind the key (DEC-093)"
            )
            assert reading.displays[entry.key] in reading.panel, (
                f"F1's panel does not list {entry.key} {where}, so the key would be bound, "
                "drawn nowhere, and reachable only by knowing it is there"
            )
            assert entry.label in reading.panel, (
                f"F1's panel does not carry {entry.label!r} {where} (DEC-007: one wording)"
            )
